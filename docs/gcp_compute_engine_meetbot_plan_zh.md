# GCP Compute Engine Meetbot 切换计划（Private-Only VM）

## 概述

本方案将 `voxella-attendee` 的 meetbot 运行时切换到 Google Cloud Platform 的 Compute Engine，采用“每个 bot 一台短生命周期 VM”的方式，不切到 GKE。

实现重点是复用当前仓库已经存在的运行时骨架：

- `launch_bot` 的 method 分流
- `BotRuntimeLease` 的状态机
- scheduler 的对账和兜底清理
- heartbeat 激活与 completion 回调
- 录制交接文档里定义的 chunk 完成边界

同时，GCP VM 方案要求默认不分配 public IP，改为 private-only 部署。

## 目标与边界

目标是让创建 bot、启动、运行、结束和清理形成一条完整链路，并满足以下约束：

- create bot 时可以指定 `region`
- 可以提前知道 region 的可用 quota 上限和可售容量
- 不在 create bot 时同步调用 GCP API 做实时 quota 计算
- 生命周期管理参考现有 `LAUNCH_BOT_METHOD=digitalocean-droplet`
- 会议结束后尽快释放 VM，避免持续计费
- 添加关键流程日志，便于排障和审计

本次不做：

- 不切到 GKE
- 不把 zone 暴露给 API 调用方
- 不把 GCP quota 查询放进用户请求的同步路径

## API 与数据结构

### `POST /api/v1/bots`

新增 `runtime_settings.region`：

- 如果用户显式传入，则优先使用
- 如果不传，使用服务端默认 region
- 如果 region 不在当前可用缓存里，返回明确错误

### `GET /api/v1/runtime-capacity`

新增只读容量接口，给前端或上游系统使用：

- `provider=gcp_compute_instance`
- 每个 region 返回 `quota_limit`
- 每个 region 返回 `quota_usage`
- 每个 region 返回 `soft_cap`
- 每个 region 返回 `effective_available`
- 每个 region 返回 `last_synced_at`

这个接口只读取本地缓存，不同步调用 GCP。

### 模型与 provider

- `BotRuntimeProviderTypes` 增加 `gcp_compute_instance`
- `LAUNCH_BOT_METHOD` 增加 `gcp-compute-engine`
- lease completion 回调改为兼容 `provider_instance_id`，保留旧字段 `droplet_id` 兼容性

## 实现方案

### 运行时 provider

新增 `bots/runtime_providers/gcp_compute_engine.py`，接口形状参考现有 DigitalOcean provider：

- `get_or_create_lease`
- `provision_bot`
- `delete_lease`
- `fetch_lease_state`
- `sync_lease`

GCP 实现建议使用官方 Python client library，而不是手写 HTTP REST。

### VM 创建方式

采用自定义镜像加 instance template 的方式：

- 模板固化网络、子网、service account、disk auto-delete、基础 labels
- 每次创建实例时只注入 bot 级动态 metadata
- VM 启动后写入 `/etc/attendee/runtime.env`
- 通过 systemd 启动 `attendee-bot-runner.service`

### 私网-only 网络

GCP VM 不挂 public IP 作为显式要求，而不是默认假设：

- 创建实例时不配置 external IP
- 子网启用 `Private Google Access`
- 出网统一走 `Cloud NAT`
- 运维访问通过 `IAP TCP forwarding` 或受控堡垒机
- 不保留静态公网 IP
- 不启用 deletion protection

### region / quota 策略

容量策略采用“后台缓存 + soft cap”：

- 后台定时同步 GCP quota 到本地缓存
- create bot 时只读本地缓存
- 每个 region 维护 allowlist
- 如果缓存过期，但仍有 last-known 值，则继续服务并记录 warning
- 如果真实创建触发 quotaExceeded，再把该 region 或 zone 降级

zone 不对外暴露，provider 在 region 内按预配置 zone 列表自动尝试。

### 生命周期管理

生命周期参考现有 DO lease 模式：

- provision 成功后 lease 进入 `provisioning`
- 首次 heartbeat 后转 `active`
- runner completion 回调触发 delete
- scheduler 对 post-meeting bots 做兜底回收
- heartbeat timeout / never launched 也触发兜底删除

### 录制与结束

和现有 chunk handoff 文档对齐：

- chunk 模式下，`recording.complete` 返回 2xx 后即可清理 VM
- 不等待 transcription / preprocess 完成
- local-file 模式保持原有 cleanup 逻辑

## 日志要求

关键节点都要打日志，并带上 `bot_id`、`bot.object_id`、`lease_id`、`provider_instance_id`：

- create bot 选择了哪个 region
- quota 缓存命中情况
- VM provision 请求和返回结果
- zone failover
- 首次 heartbeat 激活 lease
- completion callback 收到的退出码和状态
- delete 成功或失败
- 清理兜底命中原因

## 测试计划

- serializer 测试
  - `runtime_settings.region` 的合法性与默认值
  - region 无容量时返回明确错误
  - `runtime-capacity` 返回缓存数据
- provider 测试
  - 创建 VM 的 payload 不包含 public IP
  - metadata、requestId、zone 选择逻辑正确
  - delete 幂等
  - zone failover 正常
- 生命周期测试
  - heartbeat 激活 lease
  - completion 回调触发删除
  - scheduler 兜底删除
  - heartbeat timeout 和 never launched 清理
- 回归测试
  - DO 路径不受影响
  - kubernetes 路径不受影响

## 前置运维要求

在真正切换流量前，GCP 侧需要先准备好：

- private bot subnet
- `Private Google Access`
- `Cloud NAT`
- IAP 相关 IAM 和 firewall 规则，或堡垒机方案
- 预制镜像或 instance template

如果这些前置条件没准备好，private-only VM 会卡在拉镜像、访问 Google API 或运维登录这几个环节。

## 参考文档

- [DigitalOcean Droplet 生命周期文档](./digitalocean_droplet_workflow_zh.md)
- [Attendee 分片上传与 Voxella API 交接](./attendee_chunk_upload_refactor_and_voxella_api_handoff_zh.md)
- GCP Compute Engine IP 行为: https://docs.cloud.google.com/compute/docs/ip-addresses
- Cloud NAT: https://docs.cloud.google.com/nat/docs/public-nat
- Private Google Access: https://docs.cloud.google.com/vpc/docs/private-google-access
- IAP TCP forwarding: https://docs.cloud.google.com/iap/docs/using-tcp-forwarding
- Compute Engine API best practices: https://docs.cloud.google.com/compute/docs/api/best-practices
