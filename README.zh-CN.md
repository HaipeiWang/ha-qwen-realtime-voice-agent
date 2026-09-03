<p align="center">
  <img src="openai_realtime_voice_agent/icon.png" alt="适用于 Home Assistant Voice PE 的通义千问 Omni Realtime 语音助手" width="160"/>
</p>

<p align="center">
  <a href="README.md">English</a> · <strong>简体中文</strong>
</p>

# 适用于 Home Assistant Voice PE 的通义千问 Realtime 语音助手

## 它能做什么

- 将 Home Assistant Voice PE 变成原生 **Qwen Realtime** 语音助手，不再经过
  独立的 Whisper 和 Piper 链路。
- 理解自然语音、流式播放回复，通过 Voice PE 灯光显示会话状态，并支持中心按键
  打断和连续对话。
- 自动发现 Home Assistant MCP 工具，只控制向 Assist 公开的实体，包括灯、开关、
  窗帘、空调及其他受支持设备。
- 根据实体的真实能力自动生成空调模式、风速、扫风、风扇预设和选项选择工具。
- 对常见设备控制进行确定性路由，并在 Home Assistant 返回真实执行结果后才确认成功。
- 当设备名称准确且唯一、但 Home Assistant 继承的区域错误时优先采用设备名称。例如，
  通过同一 Bluetooth Proxy 接入、却被归入错误房间的客厅灯仍可被正确选中。
- 对流式音频进行匀速发送和缓冲，支持连接恢复及可选诊断录音。

## 0.10.0-beta.4 更新内容

- 同时兼容旧版 Home Assistant MCP 工具名和 Home Assistant Core 2026.9 引入的
  命名空间工具名。
- 新版 Core 自动使用 `homeassistant__GetLiveContext`，旧版自动回退到
  `GetLiveContext`。
- 执行时保留 MCP 返回的真实工具名；路由、中文工具说明、循环保护和实体选择器则使用
  稳定的统一名称。
- 保留“准确唯一名称优先”和“移除冲突区域”的 Harness 规则，不会因为兼容新版
  工具名而再次选错设备。

## 工作原理

Add-on 将配套 Voice PE 固件直接连接到阿里云百炼 Qwen Realtime。麦克风音频通过
千问原生 Realtime WebSocket 上传，语音回复流式返回 Voice PE；设备操作则通过 MCP
以及根据 Assist 已公开实体自动生成的能力工具完成。

```text
Voice PE 定制固件                       Home Assistant OS Add-on
┌────────────────────────┐  PCM / WS  ┌─────────────────────────────┐
│ 唤醒词 + 麦克风         │ ─────────▶ │ Qwen Omni Realtime 桥接服务 │
│ 扬声器 + LED 状态       │ ◀───────── │ 匀速音频 + 状态机           │
└────────────────────────┘            └──────────────┬──────────────┘
                                                    │ MCP + 自动生成工具
                                                    ▼
                                           Home Assistant 实体
```

## 使用条件

- 能够访问 Add-on Store 的 Home Assistant OS。
- 一台刷入配套定制固件的 Home Assistant Voice PE。
- 已开通受支持 Qwen Realtime 模型的阿里云百炼工作空间，以及你自己的 API Key
  和 Workspace ID。
- Home Assistant 的 **Model Context Protocol Server** 集成。

本仓库不包含任何 API Key、Workspace ID、Home Assistant 令牌、Wi-Fi 凭据、设备地址、
录音或用户实体数据。

## Realtime 模型下拉菜单

Add-on 的配置页面提供以下原生 WebSocket 模型：

| 模型 | 推荐测试用途 | Home Assistant 工具 | 联网搜索 |
| --- | --- | --- | --- |
| `qwen-audio-3.0-realtime-flash` | 低成本语音助手（默认） | 支持 | 不支持 |
| `qwen-audio-3.0-realtime-plus` | 更高质量语音助手 | 支持 | 不支持 |
| `qwen3.5-omni-flash-realtime` | 快速全模态语音助手 | 支持 | 支持* |
| `qwen3.5-omni-plus-realtime` | 最高质量全模态助手 | 支持 | 支持* |

\* 千问不能在同一会话同时启用联网搜索和 Function Calling。控制设备时请保持
Home Assistant 工具开启；测试 Omni 联网搜索时请使用独立的“仅搜索”配置。

音色按模型族分开选择：Qwen-Audio 使用独立的 `longan*` 下拉菜单（或声音复刻
voice_id），Qwen3.5 Omni 使用只包含 Omni 音色的下拉菜单。后端还会校验旧配置
或手工 YAML 修改；发现音色与模型不兼容时会记录明确错误并回退到该模型族默认音色。

## 安装 Add-on

1. 在 Home Assistant 中依次打开 **设置 → Add-ons → Add-on Store → ⋮ → 仓库**。
2. 添加以下仓库地址：

   ```text
   https://github.com/HaipeiWang/ha-qwen-realtime-voice-agent
   ```

3. 安装 **Qwen Realtime Voice Agent**。仓库有意不绑定固定容器镜像，HAOS
   会使用随附的 Dockerfile 为当前主机架构构建 Add-on，首次构建可能需要几分钟。
4. 打开 Add-on 的 **配置** 页面，填写你自己的：

   - Qwen API Key
   - Qwen Workspace ID
   - 与工作空间一致的地域，通常为 `cn-beijing`
   - 工作空间已开通的 Realtime 模型和音色

5. 保存配置。在按照下一节启用 MCP 之前，请先不要启动 Add-on。

## 启用 Home Assistant 工具

1. 通过 **设置 → 设备与服务 → 添加集成** 添加
   **Model Context Protocol Server**。
2. 在 **设置 → 语音助手 → 公开** 中，仅公开允许 Voice PE 控制的实体。
3. 对于标准 HAOS 安装，请将 Add-on 中的 **MCP server URL** 和 **Access token**
   留空，Add-on 会获得 Supervisor 提供的内部令牌。
4. 保持 **自动生成工具** 开启。Add-on 启动时会：

   - 获取官方 MCP 工具；
   - 通过兼容的 `GetLiveContext` 工具确定向 Assist 公开的实体边界；
   - 读取这些实体在 Home Assistant 中的能力；
   - 生成并注册相应设备支持的扩展函数工具。

启动 Add-on。健康运行时，日志中应出现类似内容：

```text
Home Assistant MCP Client initialized
Entity catalog built through ...GetLiveContext: ... exposed entities
Auto-generated ... capability tools
Qwen Realtime Service created
Starting WebSocket server and pipeline
```

## 连接 Voice PE

设备端固件位于
[HaipeiWang/home-assistant-voice-pe-qwen](https://github.com/HaipeiWang/home-assistant-voice-pe-qwen)。
请通过 **ESPHome Device Builder** 使用该仓库提供的 DHCP 或静态 IP 配置刷入定制固件。
首次替换原厂固件通常需要 USB，之后可以使用 OTA 更新。

配套固件默认连接：

```text
ws://homeassistant.local:8080/
```

如果你的网络无法解析这个主机名，请将固件的 `substitutions.va_url` 改为 HAOS
主机名或地址以及 Add-on WebSocket 端口。固件 URL 的端口必须与 Add-on 的
`websocket_port` 保持一致。

Voice PE 启动后应连接 Add-on 并进入空闲 LED 状态。使用唤醒词前，必须在
**设置 → 设备与服务** 中配置自动发现的 ESPHome 设备，并输入固件使用的 API
加密密钥。配套固件只在这条经过鉴权的 Home Assistant API 连接建立后启动唤醒词
检测。完成配对后再说出唤醒词和指令，并在 Add-on 日志中确认出现转写、千问响应
以及相应的工具执行记录。

完整的安装、验证、调优和故障排查方法请参阅
[DOCS.md](openai_realtime_voice_agent/DOCS.md)。该详细文档目前为英文，Add-on
配置字段本身已提供简体中文说明。

## 项目来源

- Voice PE 协议及固件集成：
  [HaipeiWang/home-assistant-voice-pe-qwen](https://github.com/HaipeiWang/home-assistant-voice-pe-qwen)，
  fork 自 [xandervanerven/home-assistant-voice-pe](https://github.com/xandervanerven/home-assistant-voice-pe)
- 后端 Add-on 基础：
  [fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime)
- 运行框架：[Pipecat](https://github.com/pipecat-ai/pipecat)

固件与后端仓库承担不同职责，因此本仓库同时注明两项来源，而不将项目描述为只来源于
其中一方的 GitHub Fork。

## 许可证

MIT，参见 [LICENSE](LICENSE)。
