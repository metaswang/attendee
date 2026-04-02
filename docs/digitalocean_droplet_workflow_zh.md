# DigitalOcean Droplet 模式端到端 Workflow（`LAUNCH_BOT_METHOD=digitalocean-droplet`）

本文描述在控制面（通常为 `myvps`）上设置 `LAUNCH_BOT_METHOD=digitalocean-droplet` 时，从调度/启动、入会录制、到对象存储上传、以及各类回调与通知的完整数据流与职责划分。

## 架构角色

| 组件 | 职责 |
|------|------|
| 控制面（Django + Celery + `run_scheduler`） | 创建 Bot、触发启动、调用 DigitalOcean API、接收 lease 完成回调、周期对账并删除 Droplet、投递 Webhook 等 |
| DigitalOcean Droplet（每 Bot 一台） | 从快照启动，经 cloud-init 注入环境，`attendee-bot-runner` 拉容器执行 `manage.py run_bot` |
| Postgres / Redis | 与运行模式无关的持久化与 Bot 侧 Redis 订阅（远程 Bot 仍连控制面配置的地址） |
| 对象存储 | 录制文件或分片上传到 Attendee 配置的存储（如 S3/R2/Azure） |

与默认 Celery 在本机跑 `run_bot` 不同：**实际执行 `BotController.run()` 的进程在 Droplet 上的容器内**，控制面只负责Provision 与回收。

---

## 1. 启动入口（谁触发 `launch_bot`）

### 1.1 即时入会（API / Dashboard）

1. `create_bot` 在事务内创建 `Bot`、`Recording`，若初始状态为 `READY` 则发出 `JOIN_REQUESTED`，状态进入 `JOINING`。
2. `BotListCreateView.post`（或 Dashboard `CreateBotView`）在 `bot.state == JOINING` 时调用 `launch_bot(bot)`。

参见 `bots/bots_api_utils.py` 中 `JOIN_REQUESTED` 的创建，以及 `bots/bots_api_views.py` / `bots/projects_views.py` 中对 `launch_bot` 的调用。

### 1.2 定时/预约 Bot（Scheduler）

1. 管理命令 `run_scheduler` 按间隔轮询（默认 60s），调用 `_run_scheduled_bots()`。
2. 满足条件的 `SCHEDULED` Bot（`join_at` 在可接受时间窗内）会 **`launch_scheduled_bot.delay(bot_id, join_at_iso)`** 入 Celery 队列。
3. 任务 `launch_scheduled_bot`：校验积分与状态后，发出 **`STAGED`** 事件，再调用 **`launch_bot(bot)`**。

预约 Bot 不会在创建当下启动 Droplet；只有到达调度窗口才进入 `STAGED` 并 Provision。

### 1.3 `launch_bot` 在 Droplet 模式下做什么

当 `LAUNCH_BOT_METHOD=digitalocean-droplet` 时，`launch_bot` **不会** `run_bot.delay`（与默认 Celery 路径不同）。它会：

1. 实例化 `DigitalOceanDropletProvider`。
2. `get_or_create_lease`：`Bot` 与 `BotRuntimeLease` 一对一，写入 provider、region、机型快照等。
3. `provision_bot`：向 DigitalOcean `POST /v2/droplets` 创建 Droplet，请求体含快照镜像、区域、`user_data`（cloud-init）等。
4. 失败时：`BotRuntimeLease.mark_failed`，写入 `FATAL_ERROR` / `FATAL_ERROR_BOT_NOT_LAUNCHED`。

代码：`bots/launch_bot_utils.py`、`bots/runtime_providers/digitalocean.py`。

---

## 2. Provision：Lease 与 cloud-init

### 2.1 Lease 记录

- 新 Droplet 创建成功后，`lease.status = provisioning`，`provider_instance_id` 为 DO 返回的 droplet id。
- `metadata` 中保存 API 响应与请求快照（不含完整 `user_data` 原文以避免日志过大）。

### 2.2 注入到 Droplet 的环境（`runtime.env`）

`_serialized_runtime_env` 会把控制面当前进程环境中**允许的**变量导出到 `/etc/attendee/runtime.env`，并强制追加例如：

- `BOT_ID`、`BOT_OBJECT_ID`
- `BOT_RUNTIME_PROVIDER=digitalocean_droplet`
- `IS_DROPLET_BOT_RUNNER=true`
- `LEASE_ID`、`LEASE_SHUTDOWN_TOKEN`
- `LEASE_CALLBACK_URL`：指向控制面 **内部** 路由  
  `POST /internal/bot-runtime-leases/<lease_id>/complete`（由 `build_site_url` + `reverse("bots_internal:bot-runtime-lease-complete")` 生成）

明确 **排除** 的环境：`DROPLET_API_KEY` 等（Droplet 不需要 DO API Token）。

### 2.3 cloud-init 行为摘要

- 创建目录并写入 `/etc/attendee/runtime.env`
- `systemctl enable/restart attendee-bot-runner.service`

快照模板中需已安装 Docker、镜像、`attendee-bot-runner` 脚本与 systemd 单元。详见 `docs/digitalocean_droplets.md`。

---

## 3. Droplet 上：Runner → 容器 → `run_bot`

`scripts/digitalocean/attendee-bot-runner.sh`（部署在镜像中）大致流程：

1. `source /etc/attendee/runtime.env`。
2. `docker run` 使用 `BOT_RUNTIME_IMAGE`，挂载 env 文件，在容器内执行：  
   `python manage.py run_bot --botid <BOT_ID>`（内部等价于同步执行 Celery 的 `run_bot.run(bot_id)`）。
3. 记录容器退出码；若配置了 `LEASE_CALLBACK_URL` 与 `LEASE_SHUTDOWN_TOKEN`，向该 URL **POST JSON**（含 `bot_id`、`droplet_id`、 `exit_code`、`final_state`、`log_tail` 等），Header：`Authorization: Bearer <LEASE_SHUTDOWN_TOKEN>`。

因此：**lease 完成回调表示「容器内 Bot 进程已结束」**，与「业务上 post_processing 是否全部完成」是不同阶段（见下文）。

---

## 4. Bot 进程内：入会、心跳、录制

### 4.1 `BotController.run()`

- 连接 Redis（`REDIS_URL` 来自注入的环境）、建 GLib 主循环、初始化各适配器与录制管线。
- 周期 `on_main_loop_timeout` 中调用 **`set_bot_heartbeat()`**，更新 `first_heartbeat_timestamp` / `last_heartbeat_timestamp`。  
  控制面 `run_scheduler._reconcile_bot_runtime_leases` 在 `LAUNCH_BOT_METHOD=digitalocean-droplet` 时，会用 **首次心跳** 将 lease 从 `provisioning` 标为 **active**（与仅依赖 API 轮询互补）。

### 4.2 预约 Bot 在 Droplet 上的入会

- `on_main_loop_timeout` 中调用 `join_if_staged_and_time_to_join()`：当 Bot 处于 `STAGED` 且到达 `join_at`，才真正发起入会。  
  这与「Scheduler 只负责 STAGED + `launch_bot`」衔接。

### 4.3 录制与上传（cleanup 阶段）

会议结束或异常路径会走到 **`cleanup()`**（`bots/bot_controller/bot_controller.py`），与运行时模式无关，核心分支：

1. **`recording_transport == r2_chunks`（分片模式）**  
   - 通过 `RecordingChunkUploader` 上传分片；在 cleanup 中 `deliver_recording_complete_callback()`。  
   - 向客户在 Bot settings 里配置的 **`recording_complete.url`** 发送**带签名的 HTTP 回调**，`trigger` 为 **`recording.complete`**，payload 含 `session_id`、`chunk_paths` 等（`make_signed_callback_request`）。  
   - 这是 **业务侧接收「录制分片已就绪」** 的通道，与下方 Webhook 不同。

2. **常规整文件录制**  
   - 可选 `upload_recording_to_external_media_storage_if_enabled()`。  
   - 使用 `S3FileUploader` 或 `AzureFileUploader` 上传到 Attendee 配置的录制存储桶/容器；上传成功后 **`recording_file_saved`** 写 DB，并删除本地文件。

3. **post_processing 与 Webhook**  
   - 若当前状态为 `POST_PROCESSING`，cleanup 会发出 **`POST_PROCESSING_COMPLETED`** 事件，驱动状态进入 **`ENDED`**。  
   - 项目/ Bot 级 **Webhook**（如 `bot.state_change`）可收到 **`post_processing_completed`**，用于「转写与后处理完成、录制可被拉取」类通知（见 `docs/webhooks.md`）。

4. 其他：`audio_chunk_uploader`、`recording_chunk_uploader` 在 cleanup 末尾 `shutdown()`。

---

## 5. Lease 完成回调与 Droplet 删除

### 5.1 `POST /internal/bot-runtime-leases/<lease_id>/complete`

实现：`bots/internal_views.py` 中 `BotRuntimeLeaseCompletionView`。

- 校验 `Authorization: Bearer` 与 lease 的 `shutdown_token` 一致。
- 校验 body 中的 `droplet_id`（若提供）与 lease 上 `provider_instance_id` 一致（或可回填）。
- 调用对应 runtime provider 的 **`delete_lease`**：对 DigitalOcean 即 `DELETE /v2/droplets/{id}`。
- 若 `exit_code` 非 0 或 `final_state == failed`，将摘要写入 `lease.last_error`。

**注意**：Runner 在容器退出后调用此接口；若 Bot 进程已正常走完 `cleanup`，lease 删除与 Droplet 销毁通常在此刻确认。控制面 **额外的** 对账逻辑（见下）用于兜底。

### 5.2 Scheduler 对账 `_reconcile_bot_runtime_leases`

仅当 `LAUNCH_BOT_METHOD=digitalocean-droplet` 时执行：

- 遍历未 `DELETED` 的 DigitalOcean lease。
- 若 Bot 已有首次心跳且 lease 仍在 provisioning → `mark_active`。
- 若 Bot 状态属于 **`post_meeting_states()`**（`FATAL_ERROR`、`ENDED`、`DATA_DELETED`）→ **主动 `delete_lease`**，避免残留机器费用。
- 否则 `sync_lease` 与 DO API 对齐元数据。

因此：**正常结束**时既可能由 completion 回调删机，也可能由下一轮对账删机；**终态 Bot** 最终应无挂起 Droplet。

---

## 6. 通知与回调汇总

| 机制 | 触发时机 | 用途 |
|------|-----------|------|
| **Lease completion** | Droplet 上 runner 在 `run_bot` 容器退出后 POST | 控制面删除 DO 资源、记录 runner 侧退出信息 |
| **Signed `recording.complete`** | `cleanup()` 中，仅 `r2_chunks` 且配置完整 | 客户后端接收分片路径等业务数据 |
| **Webhooks**（`bot.state_change` 等） | 状态/事件变更（如 `post_processing_completed`） | 通用事件通知，可与 Attendee API 配合拉取录制/转写 |
| **Bot 事件表 / API 轮询** | 同上 | 审计与调试 |

---

## 7. 故障与清理（与 Droplet 相关）

- **`clean_up_bots_with_heartbeat_timeout_or_that_never_launched`**：对心跳超时或从未起心跳的 Bot 做终止；DigitalOcean 模式下会尝试删除对应 lease（见 `bots/management/commands/clean_up_bots_with_heartbeat_timeout_or_that_never_launched.py`）。
- **`run_correct_failed_bot_launches`**：可针对从未出现心跳的新 Bot 做纠偏，Droplet 模式会处理 `runtime_lease`（见同命令实现）。

---

## 8. 环境变量备忘（控制面）

与 Droplet 启动强相关项（不完整列表，细节见 `docs/digitalocean_droplets.md`）：

- `LAUNCH_BOT_METHOD=digitalocean-droplet`
- `DROPLET_API_KEY`、`DO_BOT_REGION`、`DO_BOT_SIZE_SLUG`（或由 Bot 侧 `runtime_size_slug()` 覆盖）、`DO_BOT_SNAPSHOT_ID`、`DO_BOT_SSH_KEY_IDS`、`DO_BOT_TAGS`
- 站点 URL 相关配置需保证 `LEASE_CALLBACK_URL` **对 Droplet 可达**（公网或专线）

---

## 9. 代码索引（便于跳转）

| 环节 | 主要文件 |
|------|-----------|
| 启动分流 | `bots/launch_bot_utils.py` |
| DO API / cloud-init | `bots/runtime_providers/digitalocean.py` |
| 定时启动 | `bots/management/commands/run_scheduler.py`、`bots/tasks/launch_scheduled_bot_task.py` |
| Droplet  runner | `scripts/digitalocean/attendee-bot-runner.sh` |
| 入口命令 | `bots/management/commands/run_bot.py`、`bots/tasks/run_bot_task.py` |
| 录制/上传/回调 | `bots/bot_controller/bot_controller.py`（`cleanup`、`deliver_recording_complete_callback`） |
| Lease 完成 | `bots/internal_views.py`、`bots/internal_urls.py` |
| Lease 状态机 | `bots/models.py`（`BotRuntimeLease*`） |

以上为当前仓库实现下的 **DigitalOcean Droplet** 全链路说明；若你与 Celery 默认路径或其它 `LAUNCH_BOT_METHOD` 对比，关键差异是 **执行 `run_bot` 的位置** 与 **通过 Lease 回调 + Scheduler 回收算力**。
