# GCP Compute Engine Meetbot Runtime 运行说明

本文描述 `LAUNCH_BOT_METHOD=gcp-compute-engine` 下，控制面如何在 GCP 上创建、运行和回收 meetbot VM，以及切换到该模式时最小需要准备的配置。

## 核心行为

- 每个 bot 对应一台短生命周期 Compute Engine VM
- VM 默认不配置 public IP
- 出网依赖 `Cloud NAT`
- 访问 Google API 依赖 `Private Google Access`
- bot 结束后通过 completion callback 或 scheduler 兜底删除 VM
- create bot 不实时查询 GCP quota，只读本地 snapshot 缓存

## 必要环境变量

### 控制面

- `LAUNCH_BOT_METHOD=gcp-compute-engine`
- `GCP_PROJECT_ID`
- `GCP_BOT_SOURCE_IMAGE`
- `GCP_BOT_DEFAULT_REGION`
- `GCP_BOT_REGIONS`
- `GCP_BOT_REGION_ZONES_JSON`

### 网络

以下二选一至少配置一个：

- `GCP_BOT_SUBNETWORK`
- `GCP_BOT_NETWORK`

如果不同 region 用不同子网，配置：

- `GCP_BOT_REGION_SUBNETWORKS_JSON`

### 规格与磁盘

- `GCP_BOT_MACHINE_TYPE`
- `GCP_BOT_BOOT_DISK_GB`
- `GCP_BOT_DISK_TYPE`

也可以按 runtime class 单独覆盖：

- `BOT_RUNTIME_CLASS_TRANSCRIPTION_ONLY_GCP_MACHINE_TYPE`
- `BOT_RUNTIME_CLASS_AUDIO_ONLY_GCP_MACHINE_TYPE`
- `BOT_RUNTIME_CLASS_WEB_AV_STANDARD_GCP_MACHINE_TYPE`
- `BOT_RUNTIME_CLASS_WEB_AV_HEAVY_GCP_MACHINE_TYPE`

### quota 缓存

- `GCP_BOT_QUOTA_METRIC`
- `GCP_BOT_REGION_SOFT_CAPS_JSON`
- `GCP_RUNTIME_CAPACITY_SYNC_ON_SCHEDULER=true`

### 可选

- `GCP_BOT_TAGS`
- `GCP_BOT_LABELS_JSON`
- `GCP_BOT_SERVICE_ACCOUNT_EMAIL`
- `GCP_BOT_SERVICE_ACCOUNT_SCOPES`
- `GCP_BOT_DEFAULT_ZONE`
- `GCP_BOT_ZONES`

## API 侧变化

### 创建 bot

`POST /api/v1/bots` 支持：

```json
{
  "meeting_url": "https://meet.google.com/abc-defg-hij",
  "bot_name": "GCP Bot",
  "runtime_settings": {
    "region": "asia-southeast1"
  }
}
```

如果不传 `runtime_settings.region`，服务端使用 `GCP_BOT_DEFAULT_REGION`。

### 查看 quota 缓存

读取：

- `GET /api/v1/runtime_capacity`
- `GET /api/v1/runtime_capacity?provider=gcp_compute_instance`

返回的是本地缓存快照，不会同步打 GCP。

## quota 缓存同步

手动同步：

```bash
uv run --python 3.11 python manage.py sync_gcp_runtime_capacity
```

如果要由 scheduler 周期执行：

- `GCP_RUNTIME_CAPACITY_SYNC_ON_SCHEDULER=true`

同步逻辑会读取：

- `GCP_BOT_REGIONS`
- `GCP_BOT_QUOTA_METRIC`
- `GCP_BOT_REGION_SOFT_CAPS_JSON`

并更新 `RuntimeCapacitySnapshot`。

## VM 生命周期

### 启动

1. `create_bot` 写入 `runtime_settings.region`
2. `launch_bot()` 根据 `LAUNCH_BOT_METHOD` 进入 GCP provider
3. provider 选择 zone，调用 Compute Engine 创建 VM
4. 启动脚本写 `/etc/attendee/runtime.env`
5. `attendee-bot-runner.service` 启动 bot 容器
6. 首次 heartbeat 将 lease 从 `provisioning` 标记为 `active`

### 结束

1. bot cleanup 结束
2. runner 调用 `/internal/bot-runtime-leases/<lease_id>/complete`
3. 控制面调用 provider 删除 VM
4. 若回调失败，scheduler 在 post-meeting 或 timeout 路径兜底删除

## private-only 前置条件

切换前需要确认：

- bot subnet 已启用 `Private Google Access`
- 对应 region 已配置 `Cloud NAT`
- service account 有创建 / 查询 / 删除实例的权限
- 如果需要 SSH 运维，已配置 `IAP TCP forwarding` 或堡垒机

如果这些没准备好，常见失败表现如下：

- 拉镜像失败
- 访问 Google API 失败
- completion callback 发不出去
- 无法进入实例排障

## 日志排查点

本次实现新增或加强了以下日志点：

- create bot 时的 `runtime region`
- GCP provision request
- zone failover
- heartbeat 激活 lease
- completion callback 收到的 `provider_instance_id`
- delete request / delete done
- scheduler 兜底删除
- quota snapshot sync 结果

## 相关文件

- [GCP 切换方案文档](./gcp_compute_engine_meetbot_plan_zh.md)
- [DigitalOcean Droplet 生命周期文档](./digitalocean_droplet_workflow_zh.md)
- [Attendee 分片上传与 Voxella API 交接](./attendee_chunk_upload_refactor_and_voxella_api_handoff_zh.md)
