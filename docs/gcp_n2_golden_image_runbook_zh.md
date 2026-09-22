# GCP N2 Golden Image 执行手册

本文把 [GCP Compute Engine Meetbot 冷启动优化 Runbook](./gcp_compute_engine_cold_start_optimization_zh.md) 里的 “GCP N2 Golden Image 构建建议” 落成仓库内可执行步骤。

> 补充：本文主线仍是 `voxella-attendee` 的 bot runtime golden image。`voxella-worker-modal-scaler` 当前也采用同类思路，把 Modal drain worker 的 Docker image 预烘焙到 GCP custom image 中，以便在 `modal.com` 资源不足时回退到 GCP VM。

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
- Python: `python3`
- 仓库已配置可在 `docker build` 中成功构建 runtime image
- 如需从 Artifact Registry 预拉镜像，builder VM 已完成 `docker` 鉴权

## 推荐自动构建流程

Docker image 必须先在 `myvps2` 构建并推送。三台 VPS、控制面的
`BOT_RUNTIME_IMAGE` 与下列参数必须使用同一个 immutable digest；不要分别构建或只比较 `latest` 标签。
然后从本机已配置的 `gcloud` SDK 执行 VM image 编排（builder 只 pull，不执行 Docker build）：

若仓库的 pyenv/虚拟环境选中了 Python 3.9，先通过 `CLOUDSDK_PYTHON` 指定已安装的
Python 3.11+（例如 `export CLOUDSDK_PYTHON="$(uv python find 3.11)"`）。否则当前 gcloud
的 `compute ssh` 子命令可能加载失败，表现为 builder 一直等待 SSH。

```bash
GCP_PROJECT_ID=<image-project> \
BOT_RUNTIME_IMAGE=catblueberry/attendee-bot-runner@sha256:<release-digest> \
scripts/gcp/build-golden-image.sh
```

默认配置：

- builder VM: `n2-standard-2`
- builder boot disk: `20GB`
- base image: `projects/ubuntu-os-cloud/global/images/family/ubuntu-2204-lts`
- image family: `attendee-bot-golden`
- storage location: `asia`
- 源码目录：`/voxella/voxella-attendee`，与控制面 `runtime_agent_env()` 的默认值一致
- `BUILD_RUNTIME_IMAGE=false`，`PULL_RUNTIME_IMAGE=true`

脚本排除部署 env、私钥和本地诊断数据。准备阶段会将源码清单写入
`/etc/attendee/runtime-source.sha256`，逐文件核对其与 Docker image 内的内容，并将
image digest、Chrome/ChromeDriver 版本和源码目录写入 `/etc/attendee/runtime-release.json`。
任一源码校验不符就停止制作 image。

`diskSizeGb` 会成为后续 GCP host VM 的 source image 最小 boot disk 要求。20GB 允许轻量实例直接使用 20GB；视频会议实例仍由 runtime class 扩展为 30GB 或 50GB。10GB 不作为生产默认值，120GB 这类过大的 builder disk 也应避免。

## Builder VM 上手工执行

在 builder VM 上拉取仓库后执行：

```bash
sudo ATTENDEE_REPO_URL=https://github.com/<org>/<repo>.git \
  ATTENDEE_GIT_REF=main \
  BOT_RUNTIME_IMAGE=asia-southeast1-docker.pkg.dev/<project>/<repo>/attendee-bot-runner@sha256:<release-digest> \
  BUILD_RUNTIME_IMAGE=false \
  PULL_RUNTIME_IMAGE=true \
  bash scripts/gcp/prepare-golden-image.sh
```

### 变量说明

- `ATTENDEE_REPO_URL`: 仓库地址，必填
- `ATTENDEE_GIT_REF`: 构建所用分支或 tag，默认 `main`
- `BOT_RUNTIME_IMAGE`: 需要预置到 golden image 的 runtime image，必填
- `BUILD_RUNTIME_IMAGE`: 默认 `false`；生产 Docker build/push 只在 `myvps2` 执行
- `PULL_RUNTIME_IMAGE`: 是否执行 `docker pull`，默认 `true`
- `DOCKER_PLATFORM`: 默认 `linux/amd64`
- `PYTHON_BIN`: 预期 Python 解释器；自动构建脚本默认传 `python3`

### 脚本行为

脚本会完成以下动作：

1. 安装 Docker、`cloud-init` 和基础工具
2. 拉取或更新仓库到指定 ref
3. 使用已经在 `myvps2` 构建、推送的 immutable Docker image
4. 执行 `docker pull $BOT_RUNTIME_IMAGE`，检查 image 内源码与待烘焙源码一致
5. 安装 `attendee-bot-runner` 和 systemd service
6. 确保 `attendee-bot-runner.service` 处于 disabled 状态
7. 输出 `df -h` 与 `/var/lib/docker` 占用，清理 apt cache、临时源码包和 Docker builder cache，再输出清理后的占用
8. 执行 `cloud-init clean --logs`
9. 清理 machine id，准备制作为 custom image

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

`BOT_RUNTIME_REDIS_URL` 必须是 GCP 可达的地址。当前生产使用
`ad.voxstudio.me:6380/0` 的 TLS 入口，与 VPS 的 `10.88.0.3:6380/0` 为同一个 Redis
实例；dev 使用 `ad.voxstudio.me:6363/0`。保留现有凭据和 DB，不要把 WireGuard 私网
地址下发到没有该网段路由的 GCP VM。

## 当前仓库行为

当前仓库中的 GCP provider 默认已按 golden image 模型收敛为最小 startup script：

- 写 `/etc/attendee/runtime.env`
- `systemctl enable --now attendee-runtime-agent.service`
- `systemctl restart attendee-runtime-agent.service`

如需手工排障回退旧的 runtime bootstrap 逻辑，可显式打开：

- `GCP_BOT_ALLOW_RUNTIME_BOOTSTRAP=true`
- `BOT_RUNTIME_ALLOW_BOOTSTRAP=true`

正常生产路径下应保持关闭。这意味着 runner/service 必须已经包含在 source image 中。

## Modal Scaler 对应 GCP Golden Image 约定

`voxella-worker-modal-scaler` 已扩展为双 provider：

- `modal`
- `gcp`

调度顺序由环境变量决定：

- 全局：`SCALER_PROVIDER_ORDER`
- 单目标覆盖：`SCALER_ASR_PROVIDER_ORDER`、`SCALER_AUDIO_TOOLS_PROVIDER_ORDER`、`SCALER_DUB_PROVIDER_ORDER`、`SCALER_VIDEO_ENCODE_PROVIDER_ORDER`

测试环境可直接使用：

```bash
SCALER_PROVIDER_ORDER=gcp
```

生产环境通常建议：

```bash
SCALER_PROVIDER_ORDER=modal,gcp
```

### 目标与资源映射

当前四类 drain target 的 GCP 运行时映射如下：

| target | Docker image | GCP 运行资源 | 说明 |
|---|---|---|---|
| `asr` | `docker.io/catblueberry/voxella-modal-transcribe-gcp:latest` | `n1-standard-4` + `1 x T4` | ASR GPU worker |
| `audio_tools` | `docker.io/catblueberry/voxella-modal-audiotools-gcp:latest` | `n1-standard-4` + `1 x T4` | 音频工具 GPU worker |
| `dub` | `docker.io/catblueberry/voxella-modal-dub-gcp:latest` | `g2-standard-4` | 使用 L4，不再使用 A10G |
| `video_encode` | `docker.io/catblueberry/voxella-modal-video-encode-gcp:latest` | `n2-standard-4` | 纯 CPU worker |

注意：

- `video_encode` 预期是 CPU，不占用 T4/L4 quota。
- golden image 构建阶段可以使用 CPU builder VM；真正运行时是否申请 GPU 由 scaler target 配置决定。

### Golden image family

每个 target 使用独立 image family，不共用单一 source image：

```bash
projects/gen-lang-client-0396714319/global/images/family/voxella-arq-asr-golden
projects/gen-lang-client-0396714319/global/images/family/voxella-arq-audio-tools-golden
projects/gen-lang-client-0396714319/global/images/family/voxella-arq-dub-golden
projects/gen-lang-client-0396714319/global/images/family/voxella-arq-video-encode-golden
```

推荐通过：

```bash
GCP_DRAIN__TARGET_SOURCE_IMAGES_JSON='{"asr":"projects/gen-lang-client-0396714319/global/images/family/voxella-arq-asr-golden","audio_tools":"projects/gen-lang-client-0396714319/global/images/family/voxella-arq-audio-tools-golden","dub":"projects/gen-lang-client-0396714319/global/images/family/voxella-arq-dub-golden","video_encode":"projects/gen-lang-client-0396714319/global/images/family/voxella-arq-video-encode-golden"}'
```

做 target 到 source image 的映射；`GCP_DRAIN__SOURCE_IMAGE` 仅保留为兼容兜底值。

### Secret 与基础配置

运行时容器环境通过 GCP Secret Manager 注入：

- dev：`GCP_DRAIN__SECRET_NAME=voxella-modal-worker-env-dev`
- prod：`GCP_DRAIN__SECRET_NAME=voxella-modal-worker-env`

对应文件：

- dev：`.env.modal.secret.dev`
- prod：`.env.modal.secret`

Docker Hub 用户名当前约定为：

```bash
DOCKER_USERNAME=catblueberry
```

可用脚本：

```bash
scripts/gcp-workers/build-push-images.sh
scripts/gcp-workers/sync-modal-secret.sh
scripts/gcp-workers/build-golden-images.sh
```

### 区域与容量约束

当前容量规划是：

- `europe-west1`：10 台 T4，5 台 L4
- `europe-west10`：10 台 T4，5 台 L4

但实际可分配仍受 GCP zone 实时资源池影响。实践上：

- T4/L4 worker 真正启动时可能出现 `ZONE_RESOURCE_POOL_EXHAUSTED`
- 因此 provider 设计必须允许 `modal -> gcp` 或 `gcp -> modal` 的顺序切换
- golden image builder 不应依赖 GPU quota，本仓库已改为 CPU builder VM 来预热镜像
