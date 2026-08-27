"""Verify websocket-boundary recording produces playable, non-empty WAVs."""

import asyncio
import struct
import tempfile
from pathlib import Path

from app.audio_recording_service import AudioRecordingService
from app.raw_audio_serializer import RawAudioSerializer
from pipecat.frames.frames import OutputAudioRawFrame


async def main():
    with tempfile.TemporaryDirectory() as directory:
        service = AudioRecordingService(enable_recording=True, output_dir=directory)
        service.start_new_session("smoke")
        serializer = RawAudioSerializer(input_sample_rate=16000)
        serializer.set_input_audio_handler(service.record_input_audio)
        serializer.set_output_audio_handler(service.record_output_audio)

        input_pcm = bytes([1, 0]) * 320
        output_pcm = bytes([2, 0]) * 480
        await serializer.deserialize(input_pcm)
        await serializer.serialize(
            OutputAudioRawFrame(
                audio=output_pcm, sample_rate=24000, num_channels=1
            )
        )
        service.stop_recording()

        input_wav = next(Path(directory).glob("input_*.wav"))
        output_wav = next(Path(directory).glob("output_*.wav"))
        assert input_wav.stat().st_size == 44 + len(input_pcm)
        assert output_wav.stat().st_size == 44 + len(output_pcm)
        with input_wav.open("rb") as handle:
            handle.seek(24)
            assert struct.unpack("<I", handle.read(4))[0] == 16000
            handle.seek(40)
            assert struct.unpack("<I", handle.read(4))[0] == len(input_pcm)
        with output_wav.open("rb") as handle:
            handle.seek(24)
            assert struct.unpack("<I", handle.read(4))[0] == 24000
            handle.seek(40)
            assert struct.unpack("<I", handle.read(4))[0] == len(output_pcm)

    print("websocket recording smoke test: ok")


if __name__ == "__main__":
    asyncio.run(main())
