# Chrome 插件式 Client Recorder 方案

## 目标

把当前“服务端 Bot 入会录制”的默认路径，替换成“用户本机 Chrome 插件本地录制并上传 chunks”。

核心目的：

- 把会议期间的持续算力从服务端转移到用户设备
- 避免每场会都拉起独立 Bot 运行时
- 尽量不让其他参会者感知到一个额外的 Bot 或平台录制动作

这条路径更准确的名字应当是：

- `client recorder`
- 不是传统意义上的 `meeting bot`

## 结论

Chrome 插件方案可行，但有明确边界：

### 能做的

- 在用户自己电脑上运行
- 采集 Chrome 中会议标签页的音频
- 读取会议页 DOM 状态、字幕、参与者基础信息
- 本地切 chunk 并直接上传对象存储

### 不能替代的

- 无人值守自动入会
- Zoom / Teams 桌面客户端全面覆盖
- 脱离用户浏览器持续运行

所以它适合做：

- 默认录制路径

不适合做：

- 唯一录制路径

## 是否会被其他参会者觉察

通常不会。

前提是插件只做本地采集，并且不触发会议平台自己的“录制/转写/共享/发言”能力。

### 不易被察觉的条件

- 不以 Bot 身份入会
- 不新增会议参与者
- 不调用平台的 recording API
- 不调用平台的 transcription / captions 开关
- 不打开用户麦克风向会议发音
- 不打开摄像头或屏幕共享
- 不向会议聊天区发消息

在这种模式下，插件只是用户本机上的一个“本地录音器”，本质上更接近：

- 本地抓取当前会议标签页声音
- 再上传到你自己的后端

### 仍然会被谁觉察

用户自己会觉察。

表现可能包括：

- 用户需要主动点击插件或页面按钮开始录制
- 浏览器可能显示当前标签页正在被捕获
- 某些实现下标签页音频行为会变化

但这通常只发生在用户本地，不是全体参会者都能看到。

### 会明显暴露的动作

以下行为不要混进这个方案里，否则就不再是“隐式 client recorder”：

- 触发 Google Meet / Zoom / Teams 自带录制
- 触发平台内转写或字幕开关
- 作为一个新账号或 Bot 入会
- 发起屏幕共享
- 往会议中播放音频
- 使用虚拟麦克风向会议注入声音

## 官方能力边界

下面这些点决定了插件能不能干净落地：

### Chrome `tabCapture`

Chrome 扩展可以采集当前标签页，但通常要求用户触发扩展动作后才能开始。

官方文档：

- https://developer.chrome.com/docs/extensions/reference/tabCapture

这意味着：

- 插件不能静默、无限制地后台抓任意标签页音频
- 最合理的 UX 是“用户点击插件开始录音”

### Chrome `offscreen`

Manifest V3 的 service worker 不能直接做 DOM / MediaRecorder 这类页面工作，适合把媒体采集逻辑放到 offscreen document。

官方文档：

- https://developer.chrome.com/docs/extensions/reference/api/offscreen

### Chrome `scripting`

如果要把现有会议适配逻辑注入到 Meet / Zoom Web / Teams 页面，需要用 `chrome.scripting` 动态注入。

官方文档：

- https://developer.chrome.com/docs/extensions/reference/scripting/

### Content Scripts

如果要读取页面 DOM、字幕、按钮状态、会议状态，需要 content script 权限与站点匹配。

官方文档：

- https://developer.chrome.com/docs/extensions/develop/concepts/content-scripts

## 平台侧通知边界

各会议平台对“平台自己的录制行为”通常会通知全体参会者。

因此，插件方案必须避开平台原生录制能力。

参考官方文档：

- Google Meet 录制会通知参与者：  
  https://support.google.com/google-workspace-individual/answer/9308681
- Microsoft Teams 录制会通知参与者：  
  https://support.microsoft.com/en-us/office/record-a-meeting-in-microsoft-teams-34dfbe7f-b07d-4a27-b4c6-de62f1348c24
- Zoom 录制会通知参与者：  
  https://support.zoom.com/hc/en/article?id=zm_kb&sysparm_article=KB0068228

推论：

- 只要你不调用这些平台的“开始录制”，其他参会者通常不会收到平台级录制提示

## 最推荐的插件架构

建议采用四层：

1. `service worker`
2. `content script`
3. `offscreen document`
4. `popup / side panel`

### 1. service worker

职责：

- 管理登录态与 token
- 接收用户点击“开始/结束”
- 调用后端 `prepare_client_recording`
- 维护会话状态
- 协调 content script 与 offscreen document

不适合放的逻辑：

- 长时间音视频处理
- 复杂 DOM 操作

### 2. content script

职责：

- 注入到会议网页
- 读取会议状态
- 读取字幕 DOM
- 识别参会者列表、标题、会议信息
- 和页面上下文通信

这里最值得复用当前仓库已有逻辑：

- [shared_chromedriver_payload.js](/Users/adamwang/Project/subdub/voxella-attendee/bots/web_bot_adapter/shared_chromedriver_payload.js)
- [google_meet_chromedriver_payload.js](/Users/adamwang/Project/subdub/voxella-attendee/bots/google_meet_bot_adapter/google_meet_chromedriver_payload.js)
- [zoom_web_chromedriver_payload.js](/Users/adamwang/Project/subdub/voxella-attendee/bots/zoom_web_bot_adapter/zoom_web_chromedriver_payload.js)
- [teams_chromedriver_payload.js](/Users/adamwang/Project/subdub/voxella-attendee/bots/teams_bot_adapter/teams_chromedriver_payload.js)

这些文件现在是给 Chromedriver / Selenium 用的，但本质上已经是浏览器内运行的会议适配脚本。

### 3. offscreen document

职责：

- 真正持有媒体采集逻辑
- 处理 `MediaRecorder`
- 切音频 chunks
- 做上传队列
- 做失败重试

这是最适合替代当前服务端 `RecordingChunkUploader` 的地方。

当前服务端 uploader 逻辑见：

- [recording_chunk_uploader.py](/Users/adamwang/Project/subdub/voxella-attendee/bots/bot_controller/recording_chunk_uploader.py)

但插件版本不应该复制服务端 boto/Azure 直连逻辑，而应该换成：

- presigned URL PUT
- 或短时上传 token

### 4. popup / side panel

职责：

- 给用户可见的开始/停止控件
- 展示当前会议状态
- 展示上传状态
- 展示是否已启用字幕

这个层必须存在，因为 `tabCapture` 本身就更适合与用户手势绑定。

## 推荐数据流

### 开始录制

1. 用户打开会议页
2. 用户点击插件“开始录音”
3. 插件识别平台和会议 URL
4. 插件请求后端 `prepare_client_recording`
5. 后端返回：
   - `session_id`
   - `audio_chunk_prefix`
   - `audio_raw_path`
   - 分片上传授权
   - `complete_client_recording` 所需签名参数
6. content script / offscreen document 开始采集
7. 每 3 到 5 秒上传一个 chunk

### 录制中

插件持续上报：

- chunk 上传成功数
- 最后活动时间
- 当前平台
- 字幕启用状态
- 本地错误

可选上报：

- 参与者列表快照
- 会议信息快照

### 结束录制

1. 用户点击停止
2. 插件 flush 最后一个 chunk
3. 插件等待全部 chunk 上传完成
4. 插件调用 `complete_client_recording`
5. 服务端复用现有 preprocess / transcribe 下游流程

## 服务端 API 建议

建议新增两条 client recorder 专用接口：

### `POST /v2/meeting/client-recording/prepare`

返回：

- `session_id`
- `audio_chunk_prefix`
- `audio_raw_path`
- `chunk_interval_ms`
- `upload_strategy`
- `complete_callback` 或 `complete_endpoint`

### `POST /v2/meeting/client-recording/complete`

请求体建议包含：

- `idempotency_key`
- `session_id`
- `meeting_url`
- `provider`
- `audio.chunk_paths`
- `audio.chunk_count`
- `audio.chunk_ext`
- `audio.chunk_mime_type`
- `audio.chunk_interval_ms`
- `audio.duration_sec`
- `audio.raw_path`
- `artifacts`

重点是：

- 下游契约尽量对齐当前 `recording.complete`

当前服务端现有的 Bot 完成回调字段见：

- [bot_controller.py#L533](/Users/adamwang/Project/subdub/voxella-attendee/bots/bot_controller/bot_controller.py#L533)

## 权限设计

必须尽量小。

建议权限原则：

- 只给会议域名匹配
- 只给必要的 `scripting`
- 只给必要的 `tabCapture`
- 只给必要的 `storage`
- 只给必要的 `offscreen`

不要一上来申请：

- `<all_urls>`
- 长期后台广泛站点权限
- 永久对象存储密钥

## 安全设计

### 不要把对象存储长期密钥放进插件

这是底线。

插件是部署到用户机器上的，任何长期密钥都等于泄露。

正确做法：

- `prepare` 时下发 presigned URLs
- 或下发仅限本次 session 的短时上传 token

### 完成回调要有幂等

插件是用户侧程序，网络中断、浏览器崩溃、刷新页面都更常见。

所以 `complete_client_recording` 必须支持：

- `idempotency_key`
- chunk 列表重复提交
- 会话恢复

## 与现有仓库的复用点

### 1. 会议平台识别与页面适配

可以直接复用或裁剪：

- Google Meet 适配逻辑
- Zoom Web 适配逻辑
- Teams 适配逻辑

### 2. 浏览器内音频 chunk 录制

当前 web adapter 已经在用 `MediaRecorder` 按固定间隔输出编码后的音频 chunk：

- [google_meet_chromedriver_payload.js#L1241](/Users/adamwang/Project/subdub/voxella-attendee/bots/google_meet_bot_adapter/google_meet_chromedriver_payload.js#L1241)
- [zoom_web_chromedriver_payload.js#L571](/Users/adamwang/Project/subdub/voxella-attendee/bots/zoom_web_bot_adapter/zoom_web_chromedriver_payload.js#L571)
- [teams_chromedriver_payload.js#L1333](/Users/adamwang/Project/subdub/voxella-attendee/bots/teams_bot_adapter/teams_chromedriver_payload.js#L1333)

这部分非常适合作为插件 PoC 的出发点。

### 3. 现有 chunk 完成契约

Bot 版本当前已经有：

- `audio_chunk_prefix`
- `audio_raw_path`
- `recording_complete` callback

相关字段定义：

- [bots/models.py#L972](/Users/adamwang/Project/subdub/voxella-attendee/bots/models.py#L972)
- [bots/models.py#L996](/Users/adamwang/Project/subdub/voxella-attendee/bots/models.py#L996)

这说明 client recorder 可以和 Bot fallback 共用下游转写流水线。

## 风险与限制

### 1. 仅覆盖 Web 会议

Chrome 插件最强覆盖的是：

- Google Meet Web
- Zoom Web
- Teams Web

它不是桌面端会议录制的完整方案。

### 2. 用户必须在线

这个方案不是无人值守。

只要用户：

- 关掉标签页
- 关闭浏览器
- 电脑休眠
- 网络断开

录制就会受影响。

### 3. 平台 DOM 变动频繁

因为依赖内容脚本与会议 DOM，适配层要持续维护。

### 4. 某些采集路径会暴露给用户

虽然别人通常觉察不到，但用户本人会看到：

- 插件正在运行
- 标签页正在被捕获
- 浏览器权限请求

这是正常现象，不应试图规避。

## 最务实的实施顺序

### Phase 1：PoC

- 只支持 Google Meet Web
- 只录混合音频
- 只做本地 chunk 上传
- 不做视频
- 不做音频回注

### Phase 2：通用 client recorder

- 增加 Zoom Web / Teams Web
- 增加字幕抓取
- 增加会话恢复与失败重试

### Phase 3：产品化

- 和账户系统打通
- 会后自动生成 transcript / summary
- 与 Bot fallback 自动切换

## 最终建议

如果你的目标是“项目别关”，Chrome 插件式 client recorder 值得做，而且应当成为默认录制路径候选。

但要把预期讲清楚：

- 它是“用户端录制代理”
- 不是“云端会议 Bot”

最合理的产品组合是：

1. 默认 `client recorder`
2. 失败时 `captions_only`
3. 最后才 `bot_audio_chunks_fallback`

这样才能真正把成本结构从“按会议数买运行时”改成“按上传和转写量付费”。
