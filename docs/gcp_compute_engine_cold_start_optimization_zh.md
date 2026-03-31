# GCP Compute Engine Meetbot 冷启动优化 Runbook

本文面向 `LAUNCH_BOT_METHOD=gcp-compute-engine`，聚焦 GCP N2 CPU VM 上的冷启动优化。

## 结论

当前架构下，冷启动慢是预期现象，因为路径是：

1. 创建全新 VM
2. Guest boot
3. startup script 写 runtime env
4. runner 启动
5. Docker 启动容器
6. bot 进程初始化并首次 heartbeat

真正需要优化的不是单一一步，而是把“启动期在线工作”尽量变成“镜像预烘焙”。

## 主要瓶颈

在 GCP N2 VM 上，典型瓶颈按优先级排序如下：

- VM provisioning + guest boot
- 容器镜像 pull / layer 解压
- Chrome / 音视频依赖初始化
- 应用首次加载和入会前准备

## 官方与社区经验

### 1. 优先使用 custom image，而不是在 startup script 里做安装

Google 官方建议把常用软件和配置 bake 进镜像，避免实例启动时执行大量安装和配置工作。

参考：

- https://cloud.google.com/compute/docs/images/image-management-best-practices
- https://medium.com/google-cloud/improving-gce-boot-times-with-custom-images-f77921a2c115

### 2. 容器镜像仓库放同 region

Artifact Registry 仓库与计算资源同区域，可以降低拉镜像延迟和跨区开销。

参考：

- https://cloud.google.com/artifact-registry/docs/repositories

### 3. Compute Engine 上不要依赖“镜像流式加载”来解决 VM 冷启动

这类能力不是当前 GCE per-VM 方案的主路径，不能替代 golden image。

### 4. 如果要进一步压即时启动延迟，应该用 warm pool / standby pool

Google 官方提供了 MIG standby pool，可以用 stopped / suspended 预热实例加速 scale-out。

参考：

- https://cloud.google.com/compute/docs/instance-groups/accelerate-mig-scale-out-with-standby-pools

## 当前仓库已做的启动优化

### Docker image builder

[Dockerfile](./../Dockerfile) 已做显式保护：

- 使用 `uv sync`
- `uv sync` 时跳过 `zoom-meeting-sdk`
- 再用 wheel-only 方式单独安装 `zoom-meeting-sdk`

原因是 `zoom-meeting-sdk` 的 source tarball 缺少 `src/zoomsdk/h/zoom_sdk.h`，不能指望 source build。

### GCP provider

[bots/runtime_providers/gcp_compute_engine.py](./../bots/runtime_providers/gcp_compute_engine.py) 已支持：

- `GCP_BOT_SOURCE_IMAGE`
- `GCP_BOT_SOURCE_IMAGE_FAMILY`
- `GCP_BOT_SOURCE_IMAGE_PROJECT`

这允许运行时从 golden image family 启动，而不是每次绑死某个 image id。

### runner 分段时间戳

[scripts/digitalocean/attendee-bot-runner.sh](./../scripts/digitalocean/attendee-bot-runner.sh) 已补充：

- `runner_started_at`
- `container_start_at`
- `container_finished_at`
- `bot_launch_requested_at`

这些字段会随 completion callback 回传，便于拆分 VM 启动和容器启动耗时。

## 推荐的 v1 优化方案

### 方案 A：golden image + pre-pulled bot image

这是优先级最高、最稳妥的方案。

golden image 中预装：

- Docker
- attendee-bot-runner
- systemd unit
- Chrome 与 bot runtime 依赖
- Pulse / GStreamer 依赖
- 业务镜像预拉取

startup script 只保留：

- 写 `/etc/attendee/runtime.env`
- restart runner service

### 方案 B：Artifact Registry 同区部署

- `asia-southeast1` 的 bot，镜像仓库放 `asia-southeast1`
- `us-central1` 的 bot，镜像仓库放 `us-central1`

### 方案 C：scheduled bot 提前预热

对 `join_at` 的 bot：

- 提前 2-5 分钟启动 VM
- 让 VM 和容器在入会时间前达到 ready

### 方案 D：即时 bot 的 warm pool

如果方案 A-C 仍不够，需要进入 warm pool：

- 维护少量预热 VM
- 用控制面任务分配而不是实例创建时注入 bot metadata

这一步会改变运行时模型，建议放到第二阶段。

## GCP N2 Golden Image 构建建议

### 推荐基础

- 机器类型：N2 系列 builder VM
- builder OS：Ubuntu 22.04
- Docker build 平台：`linux/amd64`
- Python：`3.11`

### 制镜像步骤

1. 在 builder VM 上拉取仓库
2. 执行 `docker build --platform linux/amd64 -t <bot-runtime-image> .`
3. `docker pull <bot-runtime-image>` 并验证镜像本地存在
4. 安装 `attendee-bot-runner.sh` 和 service 文件
5. 确保 `attendee-bot-runner.service` 默认 disable
6. 执行 `cloud-init clean --logs`
7. 关闭实例并制作 custom image
8. 将 image 发布到固定 image family

### 推荐 image family

- `attendee-bot-golden`

控制面配置：

- `GCP_BOT_SOURCE_IMAGE_FAMILY=attendee-bot-golden`
- `GCP_BOT_SOURCE_IMAGE_PROJECT=<image-project>`

## 监控与验收

至少记录以下分段耗时：

- `launch request -> GCE instance RUNNING`
- `instance RUNNING -> runner_started_at`
- `runner_started_at -> container_start_at`
- `container_start_at -> first heartbeat`

如果第一段最长，问题在 VM 启动。
如果第二或第三段最长，问题在镜像 / runner / 容器冷启动。

## 分阶段实施计划

### Phase 1

- 使用 golden image family
- 预拉 bot runtime image
- Artifact Registry 同区部署
- 分段日志上线

### Phase 2

- 调整 scheduled bot 的预热时间
- 采样启动耗时，建立 region / zone 基线

### Phase 3

- 评估 MIG standby pool
- 如果需要，重构为 warm VM 认领 bot 任务模型
