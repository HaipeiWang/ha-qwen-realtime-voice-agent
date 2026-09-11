"""Home Assistant tool descriptions shared by realtime adapters."""

CORE_TOOL_DESCRIPTIONS = {
    "GetLiveContext": (
        "查询 Home Assistant 中设备、实体或区域的实时状态。目标不明确、需要确认实体或"
        "用户询问当前状态时，必须先调用此工具。"
    ),
    "HassTurnOn": (
        "打开、开启或启动 Home Assistant 设备。用户要求开灯、打开开关或启动设备时，"
        "必须调用此工具后再确认。"
    ),
    "HassTurnOff": (
        "关闭、关掉或停用 Home Assistant 设备。用户要求关灯、关闭开关或停止设备时，"
        "必须调用此工具后再确认。"
    ),
    "HassLightSet": (
        "设置 Home Assistant 灯光亮度、颜色或色温。用户要求调暗、调亮或改变灯光时，"
        "必须调用此工具后再确认。"
    ),
    "HassClimateSetTemperature": (
        "设置 Home Assistant 空调的目标温度。用户说把空调设为多少度、升高或降低到"
        "某个温度时，必须使用此工具；不得使用 HassSelectOption 代替。"
    ),
    "HassFanSetSpeed": (
        "设置 Home Assistant 风扇或空气净化器的风速百分比。用户要求调节风量时，"
        "必须调用此工具后再确认。"
    ),
    "HassStopMoving": (
        "停止 Home Assistant 中正在移动的窗帘、晾衣架或其他 cover 设备。用户要求"
        "停止移动时，必须调用此工具后再确认。"
    ),
}

CORE_PROPERTY_DESCRIPTIONS = {
    "name": "设备或实体名称，例如卧室吸顶灯。",
    "area": "房间或区域名称，例如卧室。",
    "floor": "楼层名称。",
    "domain": "Home Assistant 实体域；灯使用 light。",
    "device_class": "设备类别。",
    "brightness": "灯光亮度百分比，取值 0 到 100。",
    "color": "灯光颜色名称。",
    "temperature": "灯光色温值。",
}

CORE_TOOL_PROPERTY_DESCRIPTIONS = {
    ("HassClimateSetTemperature", "temperature"): (
        "空调目标温度，单位为摄氏度，例如 24。"
    ),
}
