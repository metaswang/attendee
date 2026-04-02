# GCP Bot Worker 去 DB 化方案

## Summary

- 目标是把 **GCP VM 上的 bot worker 进程** 改成纯执行单元：不直连 PostgreSQL、不使用 Django ORM、不依赖 DB 凭据；所有状态持久化、事件落库、webhook 触发、Celery 入队都回到 Django 控制面完成。
- 采用当前确认的方向：
  - **启动配置**：`内部 Bootstrap API`
  - **运行时控制**：`保留 Redis 作为 sync 信号 + worker 通过 internal API 拉最新控制数据`
- 范围先限定在 **`LAUNCH_BOT_METHOD=gcp-compute-engine`**。本地 / Kubernetes / 旧 Celery DB 直连路径先保留，避免一次性改坏全部 runtime。

## Interface Changes

- 新增专用 runtime settings：`attendee.settings.bot_runtime`
  - 提供可启动 Django 的最小设置，但不要求 `DATABASE_URL`
  - 使用 dummy / in-memory DB backend，保证 VM 上 runtime 进程即使误触发 ORM 也能在测试中尽早暴露
- 调整 worker 启动接口：
  - `python manage.py run_bot --lease-id <LEASE_ID>`
  - GCP runner 不再以 `--botid` 作为唯一输入
- 在 `/internal/` 下新增 authenticated runtime API，统一使用：
  - `Authorization: Bearer <lease.shutdown_token>`
- 新增内部接口：
  - `GET /internal/bot-runtime-leases/<lease_id>/bootstrap`
  - `GET /internal/bot-runtime-leases/<lease_id>/control`
  - `GET /internal/bot-runtime-leases/<lease_id>/media-blobs/<object_id>`
  - `POST /internal/bot-runtime-leases/<lease_id>/bot-events`
  - `POST /internal/bot-runtime-leases/<lease_id>/bot-logs`
  - `POST /internal/bot-runtime-leases/<lease_id>/participants/events`
  - `POST /internal/bot-runtime-leases/<lease_id>/chat-messages`
  - `POST /internal/bot-runtime-leases/<lease_id>/captions`
  - `POST /internal/bot-runtime-leases/<lease_id>/audio-chunks`
  - `POST /internal/bot-runtime-leases/<lease_id>/resource-snapshots`
  - `POST /internal/bot-runtime-leases/<lease_id>/recording-file-saved`
  - 现有 `POST /internal/bot-runtime-leases/<lease_id>/complete` 保留
- 所有 POST ingest payload 都带 `idempotency_key` 或稳定 `source_uuid`，服务端必须按 endpoint 做幂等。

## Implementation Changes

### 1. Worker 侧运行模型

- 引入 `BotRuntimeClient` + `BotRuntimeSnapshot` / `BotRuntimeControlSnapshot` DTO。
- `BotController`、`DefaultUtteranceHandler`、`BotResourceSnapshotTaker`、streaming/non-streaming transcription 路径全部改为依赖 `BotRuntimeClient`，不再直接 import/use ORM manager。
- 明确 worker 禁止的能力：
  - `Bot.objects.get`
  - `refresh_from_db`
  - `save`
  - `objects.create/update_or_create/get_or_create`
  - `trigger_webhook`
  - `BotEventManager` / `BotLogManager` / `RecordingManager` 直接调用
- 运行时的 mutable state 只保存在内存快照里；Redis 收到 `sync*` 命令后，worker 调 `GET /control` 更新内存，不再查 DB。

### 2. Django 控制面落库与领域逻辑回收

- 把当前散落在 `bots/bot_controller/bot_controller.py` 和 `bots/transcription_providers/utterance_handler.py` 中的 ORM 逻辑抽到 server-side service 层，internal views 只做认证、校验、调用 service。
- 服务端职责保持与现在一致：
  - `BotEventManager.create_event` 负责 bot state transition、recording state、credits、state-change webhook
  - `Participant` / `ParticipantEvent` upsert + participant webhooks
  - `ChatMessage` upsert + chat webhook
  - `Utterance` / `AudioChunk` 创建 + transcript webhook
  - `BotLogEntry` 创建 + bot log webhook
  - `BotResourceSnapshot` 落库
  - 非流式 utterance 创建后由服务端 enqueue `process_utterance`
- `media_requests` / `chat_message_requests` 仍由现有 public API 写库；worker 只通过 `/control` 消费。
- `media_blob` 不放进 `/control` 内联 JSON；统一通过 `GET /media-blobs/<object_id>` 拉取，避免控制接口混入大二进制 payload。

### 3. 转写与音频分片链路

- Closed captions:
  - worker 不再 `Participant.get_or_create` / `Utterance.update_or_create`
  - 改为 `POST /captions`
  - 服务端按现有 closed-caption 语义 upsert utterance 并触发 `transcript.update`
- Streaming transcription:
  - `DefaultUtteranceHandler` 改为 API-backed handler
  - worker 只把 speaker、timestamp、duration、transcript 发给 Django
  - 服务端创建 participant/utterance 并触发 webhook
- Non-streaming transcription:
  - GCP runtime 强制 `USE_REMOTE_STORAGE_FOR_AUDIO_CHUNKS=true`
  - GCP runtime 强制 `FALLBACK_TO_DB_STORAGE_FOR_AUDIO_CHUNKS_IF_REMOTE_STORAGE_FAILS=false`
  - worker 继续把音频 chunk 传到 object storage，但不创建 `AudioChunk`/`Utterance`
  - 上传完成后 `POST /audio-chunks` 只提交 metadata、remote object key、participant info、recording context
  - 服务端创建 `AudioChunk`、`Utterance`，并 enqueue `process_utterance`
- 这样 worker 无 DB，Django 仍保留现有录制/转写/async transcription 数据模型和 Celery 工作流。

### 4. GCP Runtime 安全与部署

- 收紧 `gcp_compute_engine.py` 的环境变量传递策略：
  - 显式排除 `DATABASE_URL`、Postgres tunnel/SSL 变量、任何 DB 凭据
  - 只保留 runtime 必需 env：Redis、storage、adapter/browser/transcription 运行参数、`LEASE_ID`、`LEASE_SHUTDOWN_TOKEN`
- GCP runner 改为使用 `DJANGO_SETTINGS_MODULE=attendee.settings.bot_runtime`
- Bootstrap payload 中返回 worker 需要的已解密 credential 子集，只返回当前 bot 实际启用功能所需字段，不把整个 project credentials 原样下发
- 保留现有 `LEASE_CALLBACK_URL` 完成回调；新增 bootstrap/control/ingest URLs 由 provider 一并注入
- 既有 Redis pub/sub 保留；public APIs 触发 `send_sync_command` 的逻辑不改，只修改 worker 端消费方式

## Test Plan

- Internal API 认证测试：
  - 错误 `lease_id`
  - 错误 bearer token
  - token 对 lease/bot 不匹配
- Bootstrap/control contract 测试：
  - 返回字段完整
  - 只下发启用功能所需 credentials
  - media request 返回 metadata，blob 走独立 download endpoint
- Worker 无 DB smoke 测试：
  - `DATABASE_URL` 缺失时，`run_bot --lease-id` 可启动
  - 关键 runtime 流程不触发 ORM
- 事件 ingest 测试：
  - join/joined/leave/meeting ended/fatal error/recording permission granted-denied
  - 幂等重复提交不会重复建 event 或错误推进 state
- 数据 ingest 测试：
  - participant join/leave/speech/update
  - chat message upsert
  - closed caption transcript
  - streaming transcript
  - bot log
  - resource snapshot
- 非流式音频链路测试：
  - worker 上传 remote chunk 后服务端创建 `AudioChunk` + `Utterance`
  - 服务端 enqueue `process_utterance`
  - 失败重试不重复建记录
- Redis 控制面回归：
  - `sync`
  - `sync_media_requests`
  - `sync_chat_message_requests`
  - `sync_voice_agent_settings`
  - `sync_transcription_settings`
  - `pause_recording` / `resume_recording`
- GCP provider 测试：
  - runtime env 不再包含 DB 相关变量
  - 启动脚本使用新 settings 和 `--lease-id`
  - completion callback 维持兼容

## Assumptions And Defaults

- 范围仅针对 **GCP VM bot worker**；控制面 Django、Celery worker、定时任务继续正常使用 DB。
- “去掉所有 DB 操作”定义为：**VM 上 bot runtime 进程及其直接调用链不进行任何 PostgreSQL 读写，也不依赖 DB 凭据**。
- 外部用户可见 webhook 继续只由 Django 控制面发送；worker 不直接触发项目 webhook。
- Redis 仍然是运行中控制信号通道，不在本阶段替换。
- GCP runtime 默认启用 remote audio chunk storage，禁用失败回退到 DB。
- `run_bot --botid` 的 DB-backed 旧路径暂时保留给非 GCP runtime；本次不做全仓一刀切迁移。
