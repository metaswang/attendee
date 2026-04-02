# 降本转向方案

## 背景

当前项目的主要成本来自「每场会都需要拉起 Bot 运行时」这一前提。

即使已经把录制从整文件上传改到 chunk 上传，只要仍然是服务端 Bot 进会，就仍然要持续消耗：

- 浏览器或 SDK 运行时
- CPU / 内存
- 会议期间的生命周期管理
- 失败重试、回收、调度、抢占和孤儿实例清理

因此，真正要降本，不能只优化上传方式，而要把产品思路从「默认 Bot 入会」改成「默认用户端采集，Bot 只做 fallback」。

## 结论

建议按优先级切换为三层模式：

1. `captions_only`
2. `client_audio_chunks`
3. `bot_audio_chunks_fallback`

其中：

- 默认优先 `client_audio_chunks`
- 平台/权限不允许时退化到 `captions_only`
- 只有确实需要 Bot 时才使用 `bot_audio_chunks_fallback`

这比继续在 Bot 架构上做局部优化更接近止损。

## 现状

仓库里已经具备一条可用的低成本过渡路径：`audio-only + r2_chunks`。

已有事实：

- `recording_settings.transport` 已支持 `r2_chunks`
- `r2_chunks` 当前只允许 web adapter，并且只允许 `format='mp3'`
- web adapter 侧已经用 `MediaRecorder` 周期 flush 音频 chunk
- cleanup 时会等待分片上传完成并发送 `recording.complete` 签名回调
- 运行时规格已经把 `audio_only` 压到 `2c/4g`

这说明短期内不必从零开始，仓库已经能承接「音频-only + chunk」这条过渡方案。

## 三层模式

### 1. captions_only

定义：

- 不录音
- 仅采集会议平台已生成的字幕或转写

优点：

- 基础设施成本最低
- 无音频上传、无后处理
- 适合“先保命、先上线”

缺点：

- 质量依赖平台字幕
- 平台不出字幕就无数据
- 很难做高质量后处理

适用场景：

- 会议纪要
- 实时提示词
- 对精确逐字稿要求不高

### 2. client_audio_chunks

定义：

- 用户自己进入会议
- 用户设备本地录制音频
- 按 chunk 直接上传对象存储
- 服务端只负责签名、会话、转写任务和结果聚合

优点：

- 中央算力消耗大幅下降
- 不再需要“一会一 Bot 实例”作为默认路径
- 会议结束即完成交接，不需要 Bot cleanup 等复杂状态机

缺点：

- 需要处理浏览器权限或桌面端权限
- 平台兼容性取决于采集方式
- 丢失“服务端统一接管会议”的控制力

适用场景：

- 参会用户愿意安装插件、桌面端或打开网页
- 产品更像“个人助手”而不是“会议机器人”

### 3. bot_audio_chunks_fallback

定义：

- 只在用户端采集失败、无人安装客户端、或必须由机器代入会议时才启用 Bot
- Bot 只录音，不录视频
- 使用 chunk 模式直传对象存储

优点：

- 可以保留现有兼容性
- 复用现有 Attendee/Bot 能力

缺点：

- 仍有运行时成本
- 仍有进会失败、封禁、等待室、租约回收等复杂性

适用场景：

- 外部客户会议
- 用户不愿安装客户端
- 必须“无人值守自动入会”

## 非 Bot 路线

### 路线 A：浏览器内录音上传

最适合：

- Google Meet Web
- Zoom Web
- Teams Web

方式：

- 用户从你自己的网页或浏览器扩展发起录制
- 直接从浏览器里拿会议页音频或标签页音频
- `MediaRecorder` 录音并上传 chunks

优点：

- 开发快
- 与当前仓库的 `MediaRecorder + chunks` 思路一致

问题：

- 对桌面版 Zoom / Teams 支持差
- 浏览器标签页/系统音频权限有平台限制

判断：

- 可以作为最快落地的非 Bot 版本
- 但不要把它误判成“完整跨平台终局方案”

### 路线 B：桌面端 Companion App

最适合：

- Zoom 桌面端
- Teams 桌面端
- 系统音频采集要求更稳定的场景

方式：

- 做一个 Tauri / Electron / 原生 helper
- 本地抓系统音频和可选麦克风
- 编码后上传 chunks

优点：

- 比纯浏览器更稳定
- 跨会议平台更现实

问题：

- 交付和安装成本更高
- 要处理多平台音频权限和驱动差异

判断：

- 如果你们目标是真正摆脱 Bot，这通常是更靠谱的中长期主线

### 路线 C：每个参会者各自上传本地麦克风

最适合：

- 你的产品本身就是参会入口
- 多数参会者本来就在你的客户端内

方式：

- 每人只录自己的麦克风
- 各端独立上传音频 chunks
- 服务端做时间对齐、转写和汇总

优点：

- 说话人天然清晰
- 质量可能优于混音抓取

问题：

- 协作门槛高
- 少一个人就少一条音轨
- 时钟对齐和缺失补偿更复杂

判断：

- 不是第一优先级
- 适合作为高质量模式，而不是救火模式

## 推荐主方案

建议不要选“只保留 Bot，但只录音”作为终局。

推荐主方案：

1. 短期：默认改成 `bot_audio_chunks_fallback`
2. 中期：上线 `client_audio_chunks`
3. 长期：把 `client_audio_chunks` 变成默认，Bot 退化为兜底能力

理由：

- 短期可以直接复用现有仓库能力止血
- 中期才能真正把并发成本从“按会议数线性扩容”改成“主要按上传和转写扩容”
- 长期保留 Bot 只是兼容策略，不应再是产品主路径

## 对当前仓库的直接动作

### 立即执行

1. 所有新建 Bot 默认改为音频-only
2. 默认 `transport='r2_chunks'`
3. 默认关闭 debug recording
4. 禁用视频相关能力和重资源能力
5. 将 Bot 路径明确标注为 fallback，而不是默认录制模式

需要避免的高成本能力：

- 视频录制
- per-participant audio
- 实时 websocket 音频
- voice agent reserve resources
- async transcription audio chunks 这类额外双写路径

### 产品层改造

新增录制来源：

- `capture_source = captions_only | client | bot_fallback`

服务端会话 API 建议拆成：

- `prepare_client_recording`
- `complete_client_recording`
- `prepare_bot_recording`
- `complete_bot_recording`

统一下游 contract：

- 都产出 `session_id`
- 都产出 `chunk_paths`
- 都进入同一条 preprocess / transcribe 流水线

### 成本控制规则

建议在服务端加硬规则：

- 免费版只允许 `captions_only`
- 标准版默认 `client_audio_chunks`
- 只有高阶套餐或明确开关才允许 `bot_fallback`

这不是技术问题，而是商业边界问题；不把默认入口改掉，成本最终还是会回到 Bot 上。

## 风险

### 浏览器录音风险

- 用户可能不会授予权限
- 桌面客户端会议音频未必能在浏览器里稳定采到

### 桌面端方案风险

- 研发周期长于浏览器方案
- 要处理多平台系统音频权限

### Bot fallback 风险

- 仍然有进会失败与平台风控
- 仍然需要租约、心跳、清理、回调链路

## 最务实的执行顺序

### Phase 1

- 全面切到 Bot 音频-only + chunks
- 停止默认视频录制
- 把所有高成本功能设为非默认

### Phase 2

- 做浏览器端 `client_audio_chunks`
- 先只支持 Web 会议
- 统一到现有 `recording.complete` 下游契约

### Phase 3

- 评估桌面端 companion app
- 让 Zoom/Teams 桌面端逐步摆脱 Bot

## 判断标准

如果你们的目标是“项目不关”，优先级不是“最完整功能”，而是：

1. 默认路径是否不再依赖 Bot
2. 单场会议是否不再占用固定运行时
3. 转写链路是否可以复用
4. 用户是否能接受权限与安装成本

只要第 1 条没达成，成本问题只是被延后，不是被解决。
