# Zoom RTMS 测试流程（结合现有 Web UI / 本地 Docker）

本文档基于当前仓库实现，说明：

1. 现有 Web UI 上“新建 Zoom meet bot”为什么不会走到 RTMS 逻辑路径。
2. 如果目标是实际测到 RTMS 分支，应该怎么走。
3. `zoom_rtms_stream_id` 从哪里拿。
4. 如何在本地 Docker / DB / UI 中确认已经进入 RTMS 路径。

## 结论先说

当前实现里，**普通 Zoom meet bot** 和 **Zoom RTMS** 是两条不同链路：

- 普通 Zoom meet bot：走 `Bot` 创建流程，最终使用 Zoom native/web adapter。
- Zoom RTMS：走 `App Session` 创建流程，必须提供 `zoom_rtms` 负载，底层依赖 `zoom_rtms_stream_id` 非空。

因此：

- **仅通过 Web UI 的“新建 Zoom meet bot”无法进入 RTMS 路径。**
- 要测 RTMS，必须先让 Zoom 发出 `meeting.rtms_started` webhook，再由你的服务转发到 `POST /api/v1/app_sessions`。
- Web UI 在当前实现里主要用于：
  - 配置项目、API Key、Zoom App 凭据
  - 查看 `App Sessions` 列表和详情
  - 验证 RTMS 会话是否真正创建和结束

代码依据：

- RTMS 分支判定：[`bots/bot_controller/bot_controller.py`](/Users/adamwang/Project/subdub/voxella-attendee/bots/bot_controller/bot_controller.py:568)
- RTMS adapter 选择：[`bots/bot_controller/bot_controller.py`](/Users/adamwang/Project/subdub/voxella-attendee/bots/bot_controller/bot_controller.py:639)
- `CreateAppSessionSerializer` 要求 `zoom_rtms`：[`bots/app_session_serializers.py`](/Users/adamwang/Project/subdub/voxella-attendee/bots/app_session_serializers.py:35)
- App Session 创建时写入 `zoom_rtms_stream_id`：[`bots/app_session_api_utils.py`](/Users/adamwang/Project/subdub/voxella-attendee/bots/app_session_api_utils.py:61)
- UI 里 App Sessions 页面文案明确写了“Use the API to create app sessions for this project.”：[`bots/templates/projects/project_bots.html`](/Users/adamwang/Project/subdub/voxella-attendee/bots/templates/projects/project_bots.html:274)

## 当前实现中，什么条件才算“进入 RTMS 逻辑路径”

核心条件只有一个：

- `bot.zoom_rtms_stream_id` 非空

因为 controller 里直接用它判断：

```python
def is_using_rtms(self):
    return self.bot_in_db.zoom_rtms_stream_id is not None
```

代码位置：[`bots/bot_controller/bot_controller.py`](/Users/adamwang/Project/subdub/voxella-attendee/bots/bot_controller/bot_controller.py:568)

一旦它非空，`get_bot_adapter()` 就会返回 `ZoomRTMSAdapter`，而不是普通 Zoom native/web adapter。

## `zoom_rtms_stream_id` 怎么拿

不是从 UI 输入，也不是从 OAuth 回调拿。

当前实现中，`zoom_rtms_stream_id` 来自 **Zoom RTMS started webhook** 携带的 RTMS 负载，经你的服务转发到 Attendee `app_sessions` API 后写入数据库。

Attendee 期望的最小 `zoom_rtms` 结构是：

```json
{
  "meeting_uuid": "...",
  "rtms_stream_id": "...",
  "server_urls": "..." 
}
```

代码依据：

- schema 定义：[`bots/app_session_serializers.py`](/Users/adamwang/Project/subdub/voxella-attendee/bots/app_session_serializers.py:49)
- 保存到 `Bot.zoom_rtms_stream_id`：[`bots/app_session_api_utils.py`](/Users/adamwang/Project/subdub/voxella-attendee/bots/app_session_api_utils.py:75)
- RTMS adapter 取字段时兼容 snake/camel：
  - `meeting_uuid` / `meetingUuid`
  - `rtms_stream_id` / `rtmsStreamId`
  - `server_urls` / `serverUrls` / `server_url`
  代码位置：[`bots/zoom_rtms_adapter/zoom_rtms_adapter.py`](/Users/adamwang/Project/subdub/voxella-attendee/bots/zoom_rtms_adapter/zoom_rtms_adapter.py:121)

实践上，建议做法是：

- 在你的 Zoom webhook handler 里先打印原始 webhook body。
- 从里面找到包含 `meeting_uuid`、`rtms_stream_id`、`server_urls` 的那段 payload。
- 将那段 payload 规范化后，作为 `zoom_rtms` 字段发给 Attendee。

如果你不确定 Zoom 当前 webhook 的 envelope 长什么样，不要猜，直接先把原始 body 落日志。

## 推荐测试目标

建议把测试拆成两个问题：

1. “普通 Web UI 新建 Zoom meet bot”不会误入 RTMS。
2. “RTMS App Session 创建链路”可以被真实打通，并且能在 UI 中看到结果。

这样测试边界最清楚。

## 测试前置条件

### 1. 本地服务已启动

当前本地 Docker 相关容器至少应包含：

- `attendee-local-app`
- `attendee-local-worker`
- `attendee-local-scheduler`
- `voxella-local-api`
- `voxella-local-postgres`

### 2. Zoom RTMS App 已配置

参考现有文档：[`docs/zoom_rtms.md`](/Users/adamwang/Project/subdub/voxella-attendee/docs/zoom_rtms.md:26)

至少需要：

- Zoom Developer Portal 中创建 RTMS App
- 订阅 `RTMS started` 和 `RTMS stopped`
- 配置 Zoom webhook endpoint
- 给 App 加 RTMS 所需 scopes
- 在 Zoom 客户端里把该 App 加到账号并允许共享实时会议内容

### 3. Attendee 项目已配置

在 Web UI 中完成：

1. 新建 project
2. 在 `Settings -> Credentials` 配置 Zoom App 凭据
3. 在 `Keys` 页创建 API Key
4. 记录 project API key，后面创建 `app_sessions` 要用

说明：

- `App Sessions` 导航默认只有在组织的 `is_app_sessions_enabled` 为 `true` 时才显示。
- 第一次成功调用 `POST /api/v1/app_sessions` 后，这个标记会被自动打开。

代码依据：[`bots/app_session_api_views.py`](/Users/adamwang/Project/subdub/voxella-attendee/bots/app_session_api_views.py:108)

## 测试流程 A：证明“Web UI 新建 Zoom meet bot”不会走 RTMS

这是负向验证，用来澄清产品行为。

### 步骤

1. 在 Web UI 里正常新建一个 Zoom meet bot。
2. 填入 Zoom meeting URL。
3. 不做任何 RTMS webhook 转发。
4. 创建 bot 并启动。

### 预期

- 该 bot 会作为普通 Zoom bot 创建。
- 数据库中的 `zoom_rtms_stream_id` 为空。
- 运行时不会选择 `ZoomRTMSAdapter`。

### 验证方法

可在本地执行：

```bash
docker exec attendee-local-app sh -lc \
  "cd /attendee && python manage.py shell -c \"from bots.models import Bot; b=Bot.objects.order_by('-id').first(); print(b.object_id, b.zoom_rtms_stream_id, b.session_type)\""
```

如果是普通 bot，预期类似：

```text
bot_xxx None 1
```

其中：

- `zoom_rtms_stream_id` 应为 `None`
- `session_type` 应为普通 bot。当前代码里 `SessionTypes.BOT = 1`，`SessionTypes.APP_SESSION = 2`

## 测试流程 B：真实打通 RTMS 路径

这是正向验证，目标是实际进入 RTMS 分支。

### 总链路

```text
Zoom 会议中打开 RTMS App
-> Zoom 发送 meeting.rtms_started webhook 到你的服务
-> 你的服务提取 RTMS payload
-> 调 Attendee POST /api/v1/app_sessions
-> attendee 创建 App Session，并写入 zoom_rtms_stream_id
-> runtime 启动后走 ZoomRTMSAdapter
-> Web UI 的 App Sessions 页面可见该会话
```

## 步骤 1：准备一个接收 Zoom webhook 的入口

当前仓库下，`voxella-api` 已经有 Zoom webhook 入口：

- `POST /api/v1/integrations/zoom/webhook`

代码位置：[`app/routers/zoom.py`](/Users/adamwang/Project/subdub/voxella-api/app/routers/zoom.py:291)

这个入口当前职责是：

- 校验 Zoom webhook 签名
- 对支持的 Zoom 事件进行处理或转发

注意：

- 你需要确认自己的应用层 handler 里，针对 `meeting.rtms_started` 事件，最终有逻辑调用 Attendee 的 `POST /api/v1/app_sessions`
- 如果当前上层应用还没接这个逻辑，就需要临时加一个 bridge handler，或手动用 curl 模拟

## 步骤 2：收到 `meeting.rtms_started` 后，构造 `app_sessions` 请求

Attendee 侧创建入口：

- `POST /api/v1/app_sessions`

代码位置：[`bots/app_session_api_views.py`](/Users/adamwang/Project/subdub/voxella-attendee/bots/app_session_api_views.py:84)

一个最小请求示例：

```json
{
  "zoom_rtms": {
    "meeting_uuid": "YOUR_MEETING_UUID",
    "rtms_stream_id": "YOUR_RTMS_STREAM_ID",
    "server_urls": "YOUR_SERVER_URLS"
  },
  "recording_settings": {
    "format": "mp4"
  },
  "transcription_settings": {
    "meeting_closed_captions": {}
  },
  "metadata": {
    "source": "zoom_rtms_test"
  }
}
```

本地手动验证可直接用：

```bash
curl -X POST http://localhost:8000/api/v1/app_sessions \
  -H "Authorization: Token <PROJECT_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "zoom_rtms": {
      "meeting_uuid": "REPLACE_ME",
      "rtms_stream_id": "REPLACE_ME",
      "server_urls": "REPLACE_ME"
    },
    "recording_settings": {
      "format": "mp4"
    },
    "transcription_settings": {
      "meeting_closed_captions": {}
    },
    "metadata": {
      "source": "manual_rtms_test"
    }
  }'
```

## 步骤 3：确认 App Session 已创建

成功后返回体里会带：

- `id`
- `zoom_rtms_stream_id`
- `state`

示例定义见：[`bots/app_session_api_views.py`](/Users/adamwang/Project/subdub/voxella-attendee/bots/app_session_api_views.py:50)

同时 DB 中会写入：

- `meeting_url = "app_session"`
- `session_type = APP_SESSION`
- `zoom_rtms_stream_id = zoom_rtms.rtms_stream_id`

代码依据：[`bots/app_session_api_utils.py`](/Users/adamwang/Project/subdub/voxella-attendee/bots/app_session_api_utils.py:61)

## 步骤 4：在 Web UI 验证会话出现

当第一次 `app_sessions` 创建成功后，Web UI 左侧会出现：

- `App Sessions`

模板位置：[`bots/templates/projects/sidebar.html`](/Users/adamwang/Project/subdub/voxella-attendee/bots/templates/projects/sidebar.html:139)

进入项目的 `App Sessions` 页面后，应该能看到刚创建的会话。

如果列表为空，页面文案也直接说明了当前产品约束：

- `No app sessions found. Use the API to create app sessions for this project.`

模板位置：[`bots/templates/projects/project_bots.html`](/Users/adamwang/Project/subdub/voxella-attendee/bots/templates/projects/project_bots.html:274)

## 步骤 5：确认 runtime 走到了 RTMS 分支

这是最关键的验证。

### 方法 1：查数据库

```bash
docker exec attendee-local-app sh -lc \
  "cd /attendee && python manage.py shell -c \"from bots.models import Bot, SessionTypes; b=Bot.objects.filter(session_type=SessionTypes.APP_SESSION).order_by('-id').first(); print('object_id', b.object_id); print('zoom_rtms_stream_id', b.zoom_rtms_stream_id); print('session_type', b.session_type); print('settings.zoom_rtms', (b.settings or {}).get('zoom_rtms'))\""
```

预期：

- `zoom_rtms_stream_id` 非空
- `settings.zoom_rtms` 存在

### 方法 2：查 runtime snapshot

`zoom_rtms_stream_id` 会被下发到 runtime snapshot：

- snapshot 序列化：[`bots/internal_views.py`](/Users/adamwang/Project/subdub/voxella-attendee/bots/internal_views.py:565)
- runtime 读取：[`bots/runtime_snapshot.py`](/Users/adamwang/Project/subdub/voxella-attendee/bots/runtime_snapshot.py:461)

### 方法 3：查日志

查 `attendee-local-app` 或对应 runtime 容器日志，关注 RTMS 关键字：

```bash
docker logs --since 30m attendee-local-app 2>&1 | rg "zoom_rtms_stream_id|rtms|App Session|app_session"
```

如果 runtime 已跑起来，再查 worker/runtime 日志：

```bash
docker logs --since 30m attendee-local-worker 2>&1 | rg "rtms|RTMSClient|stream_id|meeting_uuid"
```

RTMS adapter 侧会使用：

- `meeting_uuid`
- `rtms_stream_id`
- `server_urls`

代码位置：[`bots/zoom_rtms_adapter/zoom_rtms_adapter.py`](/Users/adamwang/Project/subdub/voxella-attendee/bots/zoom_rtms_adapter/zoom_rtms_adapter.py:134)

## 步骤 6：结束 RTMS 会话

RTMS 停止时，你的服务应处理 Zoom 的 `meeting.rtms_stopped` 事件，并调用：

- `POST /api/v1/app_sessions/end`

Attendee 侧会用 `zoom_rtms.rtms_stream_id` 去匹配已有 `App Session`。

代码位置：[`bots/app_session_api_views.py`](/Users/adamwang/Project/subdub/voxella-attendee/bots/app_session_api_views.py:139)

## 推荐的最小可执行测试方案

如果你想最快验证“代码真的走 RTMS”而不是继续讨论概念，建议这样做：

1. 在 Web UI 中创建 project、Zoom App 凭据、API key。
2. 在 Zoom Developer Portal 中配置 RTMS App 和 webhook。
3. 启动一个最小 webhook bridge：
   - 接收 Zoom `meeting.rtms_started`
   - 记录原始 body
   - 提取 `meeting_uuid` / `rtms_stream_id` / `server_urls`
   - 调用 `POST /api/v1/app_sessions`
4. 在 Zoom 会议里手动打开 RTMS App。
5. 去 Web UI 的 `App Sessions` 页面确认新会话出现。
6. 用 DB / logs 确认 `zoom_rtms_stream_id` 非空，且 runtime 走了 RTMS adapter。

## 常见误区

### 误区 1：把“Zoom meet bot”当成“RTMS”

不是一回事。

- Zoom meet bot：Meeting SDK / web bot 方式，属于“bot join meeting”
- RTMS：Zoom App 方式，属于“app session connect stream”

### 误区 2：以为 OBO / local recording token 和 RTMS 有关系

没有。

RTMS 不依赖 OBO。

现有文档也明确写了：

- RTMS is not affected by the OBF token deadline

参考：[`docs/zoom_rtms.md`](/Users/adamwang/Project/subdub/voxella-attendee/docs/zoom_rtms.md:99)

### 误区 3：以为 Web UI 可以直接创建 RTMS 会话

当前不行。

UI 只能查看 `App Sessions`，创建必须走 API。

## 本地联调时建议保留的日志

为避免下一次还在猜字段，建议在 webhook bridge 中至少打印：

- Zoom webhook `event`
- 原始 body 的前 2KB
- 解析后的 `meeting_uuid`
- 解析后的 `rtms_stream_id`
- 解析后的 `server_urls`
- 发往 Attendee `/api/v1/app_sessions` 的最终 JSON
- Attendee 返回的 `app_session id` 和 `zoom_rtms_stream_id`

这样一轮下来，就能准确知道问题卡在：

- Zoom 没发 webhook
- 你的 bridge 没取到字段
- Attendee schema 校验失败
- App Session 建成了但 runtime 没拉起
- runtime 拉起了但 RTMS 服务端握手失败

## 一句话总结

基于当前实现，**“从 Web UI 新建 Zoom meet bot”测不到 RTMS**；要测 RTMS，必须走 **Zoom `meeting.rtms_started` webhook -> `POST /api/v1/app_sessions` -> Web UI `App Sessions` 验证** 这条链路，而 `zoom_rtms_stream_id` 就是从这次 Zoom RTMS webhook 负载里拿到并持久化的。
