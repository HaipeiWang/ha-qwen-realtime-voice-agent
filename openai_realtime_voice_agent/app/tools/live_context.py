"""Make HA light brightness units explicit at the shared tool boundary."""

import json
import yaml


def normalize_live_context(value):
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            if not value.startswith("Live Context:") or "\n" not in value:
                return value
            header, body = value.split("\n", 1)
            try:
                rows = yaml.safe_load(body)
            except yaml.YAMLError:
                return value
            if not isinstance(rows, list):
                return value
            rows = normalize_live_context(rows)
            return header + "\n" + yaml.safe_dump(rows, allow_unicode=True, sort_keys=False)
        return json.dumps(normalize_live_context(decoded), ensure_ascii=False)
    if isinstance(value, list):
        return [normalize_live_context(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: normalize_live_context(item) if isinstance(item, (dict, list, str)) else item
              for key, item in value.items()}
    if value.get("domain") == "light" and isinstance(value.get("attributes"), dict):
        attrs = result["attributes"]
        raw = attrs.get("brightness")
        try:
            number = float(raw)
        except (ValueError, TypeError):
            return result
        if not isinstance(raw, bool) and 0 <= number <= 255:
            attrs.pop("brightness")
            attrs.update(brightness_raw_0_255=number, brightness_percent=round(number * 100 / 255),
                         brightness_unit="percent; use brightness_percent for spoken replies")
    return result
