"""Isolated native-Qwen probe: audio command -> function-call event.

This script never executes Home Assistant.  It opens a separate provider
session, registers the four core HA schemas, uploads a WAV command, and exits
successfully only if Qwen emits response.function_call_arguments.done.
"""

import argparse
import asyncio
import base64
import json
import os
from pathlib import Path
import wave

import websockets


QWEN_WORKSPACE_ID = os.environ.get("QWEN_WORKSPACE_ID", "").strip()
QWEN_REGION = os.environ.get("QWEN_REGION", "cn-beijing").strip()
QWEN_MODEL = os.environ.get(
    "QWEN_MODEL", "qwen3.5-omni-flash-realtime"
).strip()
ENDPOINT = (
    f"wss://{QWEN_WORKSPACE_ID}.{QWEN_REGION}.maas.aliyuncs.com/"
    f"api-ws/v1/realtime?model={QWEN_MODEL}"
)


def core_tools():
    selector = {
        "name": {"type": "string", "description": "设备或实体名称。"},
        "area": {"type": "string", "description": "房间或区域名称。"},
        "floor": {"type": "string", "description": "楼层名称。"},
        "domain": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Home Assistant 实体域，例如 light。",
        },
    }
    return [
        {
            "type": "function",
            "function": {
                "name": "GetLiveContext",
                "description": "查询 Home Assistant 中设备、实体或区域的实时状态。目标不明确时必须先调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": selector["name"],
                        "area": selector["area"],
                        "domain": selector["domain"],
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "HassTurnOn",
                "description": "打开、开启或启动 Home Assistant 设备。用户要求开灯时必须调用。",
                "parameters": {
                    "type": "object",
                    "properties": selector,
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "HassTurnOff",
                "description": "关闭、关掉或停用 Home Assistant 设备。用户要求关灯时必须调用。",
                "parameters": {
                    "type": "object",
                    "properties": selector,
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "HassLightSet",
                "description": "设置 Home Assistant 灯光亮度、颜色或色温。用户要求调灯时必须调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        **selector,
                        "color": {"type": "string"},
                        "temperature": {"type": "integer", "minimum": 0},
                        "brightness": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 100,
                            "description": "灯光亮度百分比，0 到 100。",
                        },
                    },
                    "required": [],
                },
            },
        },
    ]


async def receive_json(websocket, timeout=15):
    return json.loads(await asyncio.wait_for(websocket.recv(), timeout=timeout))


def read_pcm16_mono_16k(wav_path):
    with wave.open(str(wav_path), "rb") as source:
        if (
            source.getnchannels() != 1
            or source.getsampwidth() != 2
            or source.getframerate() != 16000
        ):
            raise ValueError("probe WAV must be mono PCM16 at 16 kHz")
        return source.readframes(source.getnframes())


async def run_probe(wav_path, semantic_explicit=False):
    api_key = os.environ.get("QWEN_API_KEY", "").strip()
    if not api_key:
        # docker exec does not inherit variables exported by PID 1's run.sh.
        # The migration's dedicated secret file is authoritative; fall back to
        # Supervisor options for installations that already expose the new key.
        secret_path = Path("/data/qwen_api_key")
        if secret_path.is_file():
            api_key = secret_path.read_text(encoding="utf-8").strip()
        else:
            options = json.loads(Path("/data/options.json").read_text(encoding="utf-8"))
            api_key = str(options.get("qwen_api_key") or "").strip()
    if not api_key:
        raise RuntimeError("Qwen API key is unavailable to the isolated probe")
    if not QWEN_WORKSPACE_ID:
        raise RuntimeError("Qwen Workspace ID is unavailable to the isolated probe")
    async with websockets.connect(
        ENDPOINT,
        additional_headers={"Authorization": f"Bearer {api_key}"},
        open_timeout=15,
        close_timeout=3,
        max_size=4 * 1024 * 1024,
    ) as websocket:
        created = await receive_json(websocket)
        if created.get("type") != "session.created":
            raise RuntimeError(f"expected session.created, got {created.get('type')}")

        await websocket.send(json.dumps({
            "type": "session.update",
            "session": {
                "modalities": ["text", "audio"],
                "instructions": (
                    "你是 Home Assistant 控制路由器。设备控制必须调用工具，"
                    "绝不能只用自然语言声称执行。工具调用前保持静默。"
                ),
                "voice": "Tina",
                "audio": {
                    "input": {"format": {"type": "pcm", "sample_rate": 16000}},
                    "output": {"format": {"type": "pcm", "sample_rate": 24000}},
                },
                "input_audio_transcription": {"model": "qwen3-asr-flash-realtime"},
                "turn_detection": (
                    {
                        "type": "semantic_vad",
                        "create_response": False,
                        "interrupt_response": False,
                    }
                    if semantic_explicit else None
                ),
                "tools": core_tools(),
            },
        }, ensure_ascii=False))

        while True:
            event = await receive_json(websocket)
            if event.get("type") == "error":
                print("ERROR", event.get("error", {}).get("code"), event.get("error", {}).get("message"))
                return False
            if event.get("type") == "session.updated":
                echoed = event.get("session", {}).get("tools", [])
                print("SESSION_UPDATED tools=", len(echoed))
                break

        pcm = read_pcm16_mono_16k(wav_path)
        if semantic_explicit:
            # Match Voice PE transport timing, then provide trailing silence so
            # server VAD emits speech_stopped without auto-creating a response.
            pcm += b"\x00\x00" * 16000
            for offset in range(0, len(pcm), 640):
                await websocket.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(pcm[offset:offset + 640]).decode("ascii"),
                }))
                await asyncio.sleep(0.02)
        else:
            await websocket.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm).decode("ascii"),
            }))
            await websocket.send(json.dumps({"type": "input_audio_buffer.commit"}))
            await websocket.send(json.dumps({"type": "response.create"}))

        while True:
            event = await receive_json(websocket, timeout=25)
            kind = event.get("type")
            if kind == "conversation.item.input_audio_transcription.completed":
                print("TRANSCRIPT", event.get("transcript"))
            elif kind == "input_audio_buffer.speech_stopped" and semantic_explicit:
                print("SPEECH_STOPPED_EXPLICIT_RESPONSE")
                await websocket.send(json.dumps({"type": "response.create"}))
            elif kind == "response.function_call_arguments.done":
                print(
                    "FUNCTION_CALL",
                    event.get("name"),
                    event.get("call_id"),
                    event.get("arguments"),
                )
                return True
            elif kind == "response.audio.done":
                print("AUDIO_DONE_WITHOUT_TOOL")
            elif kind == "response.done":
                print("RESPONSE_DONE", event.get("response", {}).get("status"))
                return False
            elif kind == "error":
                print("ERROR", event.get("error", {}).get("code"), event.get("error", {}).get("message"))
                return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("wav_path")
    parser.add_argument("--semantic-explicit", action="store_true")
    args = parser.parse_args()
    succeeded = asyncio.run(
        run_probe(args.wav_path, semantic_explicit=args.semantic_explicit)
    )
    raise SystemExit(0 if succeeded else 2)


if __name__ == "__main__":
    main()
