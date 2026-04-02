# 方案

## Attendee 分片上传重构与 Voxella API 交接

## 概述

将 `voxella-attendee` 从“本地文件录制 + 会议结束后上传/后处理”重构为“浏览器侧分片录制 + 直传 R2 + 签名完成回调”。

保持 `voxella-api` 作为会话生命周期的所有者：由其预创建会话，并在创建 attendee bot 时传入精确的 R2 路径。

会议结束时，attendee 仅保证：

- 所有音视频分片均已上传；
- `voxella-api` 已接受签名完成回调；
- 运行时随后可立即进入清理/删除流程。

复用 `voxella-api` 与 `voxella-worker-audio-preprocess` 现有的 record & transcribe 下游契约；attendee 不再执行 ffmpeg encode/decode/remux/faststart，也不等待转写完成。

## 公共接口变更

### voxella-attendee `POST /api/v1/bots`

扩展 `recording_settings`，新增 chunk 模式；默认保持 legacy 行为以兼容：

- `transport: "local_file" | "r2_chunks"`；默认 `"local_file"`
- `audio_chunk_prefix: string`，当 `transport="r2_chunks"` 时必填
- `audio_raw_path: string`，当 `transport="r2_chunks"` 时必填
- `video_chunk_prefix?: string` 可选；仅在开启视频录制时必填
- `chunk_interval_ms?: int` 默认 `5000`

扩展 `callback_settings`，新增专用完成回调配置：

- `recording_complete: { url: string, signing_secret: string }`

校验规则：

- 本阶段 `transport="r2_chunks"` 仅支持 web meeting adapters
- `audio_chunk_prefix` 必须遵循 record/transcribe 的 chunk-prefix 约定
- 音频-only bot 不得出现 `video_chunk_prefix`

### voxella-api 在既有 meeting-bot 集成面新增录制路由

`POST /v2/meeting/app/bot/recording/prepare`

- 创建由 `voxella-api` 拥有的 Session
- 返回 `session_id`、`audio_chunk_prefix`、`audio_raw_path`、可选 `video_chunk_prefix`，以及 attendee completion callback 配置

`POST /v2/meeting/app/bot/recording/complete`

- attendee 发起的 webhook 风格签名 POST
- 请求体包含 `idempotency_key`、`trigger`、bot/session 标识及分片元数据

完成回调 payload 结构：

- `trigger: "recording.complete"`
- `data.session_id`
- `data.audio`：`chunk_paths`、`chunk_count`、`chunk_ext`、`chunk_mime_type`、`chunk_interval_ms`、`duration_sec`、`raw_path`
- `data.video`（可选）：同样的分片元数据，本阶段仅作为 artifacts 存储

## 实现变更

### voxella-attendee

将当前 web-adapter 的本地录制路径替换为浏览器侧 `MediaRecorder` 分片采集，参考 `voxella-web` 的 record/upload 流程。

在 chunk 模式下移除对以下能力的依赖：

- 本地 ffmpeg 屏幕/音频编码
- seekable/faststart 清理
- 向 attendee 存储上传整文件
- 关停前等待 utterance/transcription 完成

保留独立的音频与视频分片流：

- 音频分片流始终存在，且是下游 preprocess/transcribe 的唯一输入流
- 开启视频时上传视频分片，并回传给 API；但 attendee 不做后处理

新增受限的浏览器内上传队列，并基于传入 prefix 执行免租约（lease-free）的直传 object-key PUT 到 R2/object storage。

会议结束时：

- 停止 recorder；
- flush 音/视频待上传分片；
- 发送签名完成回调；
- 回调返回 2xx 后，进入终态“后处理完成”，并立即请求运行时清理。

将 `create_debug_recording()` 默认设为 `false`；仅显式开启或全局 override 时启用。

引入运行时规格策略类：

- `transcription_only: 2c/4g`
- `audio_only: 2c/4g`
- `web_av_standard: 4c/8g`
- `web_av_heavy: 8c/16g`

根据实际 pipeline 标志推导规格类：

- 当启用 voice-agent 预留资源、按参与者音频、实时 websocket 音频或其他高成本能力时，归类为 heavy

同一规格类抽象同时用于 Kubernetes requests/limits 与 DigitalOcean droplet 规格选择。

### voxella-api

将 browser `record_complete` 中“recording-chunks -> preprocess enqueue”共享逻辑抽取为 helper，使 browser-record 与 attendee-callback 两条路径复用同一实现。

`prepare` 路由：

- 以 `source_type="record"` 创建 session，保持现有下游/UI/计费行为
- 按当前 record/transcribe 约定计算规范路径：
  - 音频分片：`customer_audio/{user_id}/{session_id}/chunks`
  - 音频 raw 路径：`customer_audio/{user_id}/{session_id}/original.m4a`
  - 可选视频分片：`video/{user_id}/{session_id}/chunks`

`complete` 路由：

- 使用 canonical JSON 做 HMAC 签名校验（风格与 attendee webhook signing 一致）
- 基于 `idempotency_key` 做幂等约束
- 加载预创建 session，将音/视频分片元数据持久化到 session artifacts
- 将 session 置为 `started/preprocess/upload_completed`
- 入队现有音频 preprocess 工作流，参数包括：
  - `ingest_mode="recording_chunks"`
  - `recording_chunk_paths = audio.chunk_paths`
  - 回调中的各项 `recording_chunk_*` 字段
  - `r2_raw_path = audio.raw_path`
- 视频元数据仅存于 artifacts 供后续使用；本阶段不入队视频处理

### voxella-worker-audio-preprocess

无需行为变更；继续按现状消费 `recording_chunks` payload。

补充/保留测试，确认 callback 驱动的 payload 结构持续兼容。

## 测试计划

### voxella-attendee

- serializer 测试：覆盖 `transport="r2_chunks"` 与 completion callback 字段
- bot 创建拒绝测试：覆盖非法路径组合与不支持的 adapter
- web-adapter 测试覆盖：
  - audio-only 分片上传
  - audio+video 分片上传
  - 最后一个分片 flush 后触发最终 callback
  - chunk 模式不执行本地文件上传/faststart 路径
- runtime policy 测试：覆盖规格类选择与 Kubernetes/DigitalOcean 映射
- debug-recording 默认关闭回归测试

### voxella-api

- `prepare` 路由测试：session 创建与规范路径生成
- `complete` 路由测试：
  - 签名校验
  - 幂等
  - session artifact 持久化
  - 复用共享 preprocess enqueue helper
  - `recording_chunks` 的 ARQ payload 正确性
- 回归测试：证明既有 browser `record_complete` 仍走同一个共享 helper

### 跨仓冒烟

- `prepare -> attendee create_bot payload -> completion callback fixture -> preprocess enqueue`
- 断言 session 状态推进到 preprocess，且入队 job payload 与当前 worker 契约一致

## 假设与默认值

本阶段仅将 web meeting adapters 迁移到 chunk 模式；native Zoom SDK/RTMS 维持原行为，或在 `transport="r2_chunks"` 下直接拒绝。

对于 A/V 会议，转写始终由上传后的音频分片流驱动；视频分片仅持久化，不在本阶段由 attendee 后处理，也不被下游 worker 消费。

completion callback 的 2xx 响应是录制交接边界；attendee 在运行时清理前不等待 preprocess/transcribe 完成。

精确的 R2 object-key prefixes 由 `voxella-api` 传入 attendee；在 chunk 模式下 attendee 不自行生成或改写路径模式。
