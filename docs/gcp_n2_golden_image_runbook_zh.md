# GCP N2 Golden Image 执行手册

本文把 [GCP Compute Engine Meetbot 冷启动优化 Runbook](./gcp_compute_engine_cold_start_optimization_zh.md) 里的 “GCP N2 Golden Image 构建建议” 落成仓库内可执行步骤。

## 目标

把以下工作预先烘焙进 GCP custom image：

- Docker
- bot runtime image
- `attendee-bot-runner`
- `attendee-bot-runner.service`
- Chrome / 音视频运行时依赖

这样控制面在实例启动时只需要：

- 写 `/etc/attendee/runtime.env`
- `systemctl restart attendee-bot-runner.service`

## 前提

- builder VM: Ubuntu 22.04, N2 系列
- 构建平台: `linux/amd64`
- Python: `3.11`
- 仓库已配置可在 `docker build` 中成功构建 runtime image
- 如需从 Artifact Registry 预拉镜像，builder VM 已完成 `docker` 鉴权

## Builder VM 上执行

在 builder VM 上拉取仓库后执行：

```bash
sudo ATTENDEE_REPO_URL=https://github.com/<org>/<repo>.git \
  ATTENDEE_GIT_REF=main \
  BOT_RUNTIME_IMAGE=asia-southeast1-docker.pkg.dev/<project>/<repo>/attendee-bot-runner:latest \
  BUILD_RUNTIME_IMAGE=true \
  PULL_RUNTIME_IMAGE=true \
  bash scripts/gcp/prepare-golden-image.sh
```

### 变量说明

- `ATTENDEE_REPO_URL`: 仓库地址，必填
- `ATTENDEE_GIT_REF`: 构建所用分支或 tag，默认 `main`
- `BOT_RUNTIME_IMAGE`: 需要预置到 golden image 的 runtime image，必填
- `BUILD_RUNTIME_IMAGE`: 是否本机执行 `docker build --platform linux/amd64`，默认 `true`
- `PULL_RUNTIME_IMAGE`: 是否执行 `docker pull`，默认 `true`
- `DOCKER_PLATFORM`: 默认 `linux/amd64`
- `PYTHON_BIN`: 预期 Python 解释器，默认 `python3.11`

### 脚本行为

脚本会完成以下动作：

1. 安装 Docker、`cloud-init` 和基础工具
2. 拉取或更新仓库到指定 ref
3. 执行 `docker build --platform linux/amd64 -t $BOT_RUNTIME_IMAGE .`
4. 执行 `docker pull $BOT_RUNTIME_IMAGE`
5. 安装 `attendee-bot-runner` 和 systemd service
6. 确保 `attendee-bot-runner.service` 处于 disabled 状态
7. 执行 `cloud-init clean --logs`
8. 清理 machine id，准备制作为 custom image

## 发布 custom image

脚本完成后，在 builder VM 上继续执行：

```bash
sudo poweroff
```

然后在本地或 CI 上执行：

```bash
gcloud compute images create attendee-bot-golden-20260331 \
  --project <image-project> \
  --source-disk <builder-vm-disk> \
  --source-disk-zone <builder-vm-zone> \
  --family attendee-bot-golden
```

如果你是从已停止实例直接制镜像，也可以改用 `--source-disk` 指向该实例的 boot disk。

## 控制面配置

发布 image family 后，控制面至少配置：

```bash
GCP_BOT_SOURCE_IMAGE_FAMILY=attendee-bot-golden
GCP_BOT_SOURCE_IMAGE_PROJECT=<image-project>
```

不要再同时设置固定的 `GCP_BOT_SOURCE_IMAGE`，否则会绕过 family。

## 当前仓库行为

当前仓库中的 GCP provider 已按 golden image 模型收敛为最小 startup script：

- 写 `/etc/attendee/runtime.env`
- `systemctl restart attendee-bot-runner.service`

这意味着 runner/service 必须已经包含在 source image 中。
