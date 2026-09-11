"""Final-input gate and name-first control validation, without provider fields."""

import asyncio
import re
from dataclasses import dataclass, field

from app.control_intent_router import CONTROL_DOMAINS, ENTITY_CONTROL_TOOLS, EntityCatalog
from app.tool_names import canonical_tool_name


class TranscriptGate:
    def __init__(self):
        self._ready = asyncio.Event()
        self.text = ""

    def finish(self, text: str) -> None:
        self.text = text.strip()
        self._ready.set()

    async def wait(self, timeout: float = 3.0) -> str:
        try:
            await asyncio.wait_for(self._ready.wait(), timeout)
        except TimeoutError:
            return ""
        return self.text


@dataclass(frozen=True)
class ControlDecision:
    allowed: bool
    arguments: dict = field(default_factory=dict)
    reason: str = ""


def normal(text: str) -> str:
    return EntityCatalog._normalise_selector(text).replace("的", "")


def light_effects(text: str) -> set[str]:
    effects = set()
    if any(w in text for w in ("亮度", "调亮", "调暗", "亮一点", "暗一点", "最亮", "最暗", "%", "百分之")):
        effects.add("brightness")
    if any(w in text for w in ("色温", "调暖", "调冷", "暖一点", "冷一点", "暖色", "冷色", "最暖", "最冷", "开尔文")):
        effects.add("temperature")
    if any(w in text for w in ("红色", "绿色", "蓝色", "紫色", "黄色")):
        effects.add("color")
    return effects


def is_state_query(text: str) -> bool:
    return bool(re.search(r"查一下|查询|查一查|查看|查下|多少|什么状态|是否|有没有", text))


def spoken_number(token: str) -> int:
    if token.isdigit():
        return int(token)
    digits = dict(zip("零一二三四五六七八九", range(10)))
    digits["两"] = 2
    total = current = 0
    for char in token:
        if char in digits:
            current = digits[char]
        else:
            total += (current or 1) * {"十": 10, "百": 100, "千": 1000}[char]
            current = 0
    return total + current


def explicit_light_values(text: str) -> dict[str, int]:
    number = r"([0-9零一二两三四五六七八九十百千]+)"
    patterns = {
        "brightness": (r"百分之\s*" + number, number + r"\s*[%％]",
                       r"亮度\s*(?:设为|设置为|调到|调为|为)?\s*" + number),
        "temperature": (number + r"\s*开尔文", r"色温\s*(?:设为|设置为|调到|调为|为)?\s*" + number),
    }
    values = {}
    for key, choices in patterns.items():
        for pattern in choices:
            match = re.search(pattern, text)
            if match:
                values[key] = spoken_number(match.group(1))
                break
    return values


class ControlArbiter:
    """Do not let an area-only model selector override a unique spoken name."""

    def __init__(self, catalog: EntityCatalog):
        self.catalog = catalog
        self.previous_name: str | None = None

    def decide(self, text: str, tool: str, arguments: dict, attributes: dict | None = None,
               *, selection_only: bool = False) -> ControlDecision:
        name = canonical_tool_name(tool)
        if name not in ENTITY_CONTROL_TOOLS:
            return ControlDecision(True, dict(arguments))
        if not text.strip():
            return ControlDecision(False, reason="final_transcript_required")
        norm = normal(text)
        if is_state_query(text):
            return ControlDecision(False, reason="state_query_does_not_authorize_write")
        if re.search(r"(?:不要|别|不用|不必|取消)", text):
            return ControlDecision(False, reason="negated_control_requires_clarification")
        if re.search(r"(?:分钟|小时|秒钟?)(?:以|之)?后|明天|定时|闹钟", text):
            return ControlDecision(False, reason="scheduling_not_implemented")
        # Until per-action plans exist, reject mixed action types before any write.
        # A single LightSet may still contain brightness and temperature together.
        switching = bool(re.search(r"打开|开启|关闭|关掉|熄灭|开灯|关灯", text))
        if switching and light_effects(text):
            return ControlDecision(False, reason="compound_actions_require_separate_requests")
        if re.search(r"先.+(?:再|然后)|(?:亮度|色温).+(?:再|然后).*(?:亮度|色温)", text):
            return ControlDecision(False, reason="sequential_actions_require_separate_requests")
        args = dict(arguments)
        if not selection_only and name in {"HassTurnOn", "HassTurnOff"}:
            verbs = ("打开", "开启", "启动", "点亮", "开一下") if name == "HassTurnOn" else ("关闭", "关掉", "关上", "熄灭", "关一下")
            prefix = "开" if name == "HassTurnOn" else "关"
            if not any(w in text for w in verbs) and not text.strip().startswith(prefix):
                return ControlDecision(False, reason="explicit_action_required")
        if name == "HassTurnOn" and any(w in text for w in ("关闭", "关掉", "熄灭")):
            return ControlDecision(False, reason="action_requires_clarification")
        if name == "HassTurnOff" and any(w in text for w in ("打开", "开启", "点亮")):
            return ControlDecision(False, reason="action_requires_clarification")
        candidates = [e for e in self.catalog.entities if e.domain in CONTROL_DOMAINS]
        hint = self.catalog.domain_hint_from_text(text)
        if hint:
            candidates = [e for e in candidates if e.domain == hint]
        explicit_all = any(w in text for w in ("全部", "所有"))
        if explicit_all:
            area = args.get("area")
            if not isinstance(area, str) or normal(area) not in norm:
                return ControlDecision(False, reason="explicit_group_target_required")
            if not any(normal(e.area) == normal(area) for e in candidates):
                return ControlDecision(False, reason="group_not_exposed")
            args.pop("name", None)
            args.pop("floor", None)
            if hint:
                args["domain"] = [hint]
            if selection_only:
                return ControlDecision(True, args)
            if name == "HassLightSet":
                if not (attributes or {}).get("group_ranges_validated"):
                    return ControlDecision(False, reason="group_light_parameters_require_per_target_validation")
                return self._validate_light(text, args, attributes)
            return ControlDecision(True, args)

        exact = [e for e in candidates if any(normal(n) and normal(n) in norm for n in (e.name, *e.aliases))]
        if len(exact) == 1:
            matches = exact
        elif exact:
            return ControlDecision(False, reason="ambiguous_name")
        else:
            # Known light form words may be omitted: 卧室的灯 -> 卧室吸顶灯.
            matches = []
            for e in candidates:
                short = re.sub(r"吸顶|吊|台|落地", "", normal(e.name)) if e.domain == "light" else normal(e.name)
                if len(short) >= 3 and short in norm:
                    matches.append(e)
            if not matches and self.previous_name and any(w in text for w in ("它", "再", "这盏", "那盏")):
                matches = [e for e in candidates if e.name == self.previous_name]
            if not matches and len(candidates) == 1 and hint:
                # A room mentioned in any known name cannot silently fall back.
                rooms = ("卧室", "客厅", "厨房", "书房", "阳台", "卫生间")
                mentioned = [r for r in rooms if r in norm]
                if not mentioned or all(r in normal(candidates[0].name) for r in mentioned):
                    matches = candidates
        if len(matches) != 1:
            return ControlDecision(False, reason="target_requires_clarification")
        target = matches[0]
        # Name+area is an AND selector in HA. Unique names own the selection.
        args["name"] = target.name
        args.pop("area", None)
        args.pop("floor", None)
        args["domain"] = [target.domain]
        if selection_only:
            return ControlDecision(True, args)
        if name == "HassLightSet":
            return self._validate_light(text, args, attributes)
        return ControlDecision(True, args)

    def _validate_light(self, text: str, args: dict, attributes: dict | None) -> ControlDecision:
        effects = light_effects(text)
        supplied = {k for k in ("brightness", "temperature", "color") if args.get(k) is not None}
        if not effects or not supplied or not effects.issubset(supplied):
            return ControlDecision(False, reason="requested_light_effect_missing")
        if effects and not supplied.issubset(effects):
            return ControlDecision(False, reason="unrequested_light_effect")
        for key, value in explicit_light_values(text).items():
            if args.get(key) != value:
                return ControlDecision(False, reason="spoken_parameter_mismatch")
        if "brightness" in supplied and "brightness" not in explicit_light_values(text):
            relative = 10 if any(w in text for w in ("调亮", "亮一点")) else -10 if any(w in text for w in ("调暗", "暗一点")) else 0
            if relative:
                current = (attributes or {}).get("brightness")
                if isinstance(current, bool) or not isinstance(current, (int, float)) or not 0 <= current <= 255:
                    return ControlDecision(False, reason="brightness_state_unknown")
                args["brightness"] = max(1, min(100, round(current * 100 / 255) + relative))
            elif "最亮" in text:
                args["brightness"] = 100
            elif "最暗" in text:
                args["brightness"] = 1
            else:
                return ControlDecision(False, reason="explicit_brightness_required")
        if "temperature" in supplied and "temperature" not in explicit_light_values(text):
            relative = -500 if any(w in text for w in ("调暖", "暖一点")) else 500 if any(w in text for w in ("调冷", "冷一点")) else 0
            if relative:
                current = (attributes or {}).get("color_temp_kelvin")
                low = (attributes or {}).get("min_color_temp_kelvin")
                high = (attributes or {}).get("max_color_temp_kelvin")
                if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (current, low, high)):
                    return ControlDecision(False, reason="color_temperature_state_unknown")
                args["temperature"] = max(low, min(high, current + relative))
            else:
                return ControlDecision(False, reason="explicit_temperature_required")
        if "brightness" in supplied:
            value = args["brightness"]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
                return ControlDecision(False, reason="brightness_out_of_range")
        if "temperature" in supplied:
            value = args["temperature"]
            low = (attributes or {}).get("min_color_temp_kelvin")
            high = (attributes or {}).get("max_color_temp_kelvin")
            if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (value, low, high)):
                return ControlDecision(False, reason="color_temperature_range_unknown")
            if not low <= value <= high:
                return ControlDecision(False, reason="color_temperature_out_of_range")
        return ControlDecision(True, args)

    def record_completed(self, arguments: dict) -> None:
        """Only successful execution may establish a follow-up target."""
        self.previous_name = arguments.get("name")
