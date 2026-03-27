# Modal Launch Bot 方案

本文档描述如何把本仓库的 bot 拉起方式切到 `LAUNCH_BOT_METHOD=modal`，并通过 Modal `Function.spawn()` 运行现有 `BotController` 流程。

## 1. 镜像

本仓库直接复用根目录 `Dockerfile` 构建运行镜像：

```bash
IMAGE_NAME=docker.io/<your-namespace>/attendee-bot:modal-v1 ./scripts/build_modal_image.sh
```

## 2. Modal 配置

准备 `.env.modal` 并填入实际值。R2 变量命名参考你给的 `../subdub/voxella-api/.env`：

- `R2__ENDPOINT`
- `R2__REGION`
- `R2__ACCESS_KEY_ID`
- `R2__SECRET_ACCESS_KEY`

将 `.env.modal` 导入 Modal Secret：

```bash
modal secret create attendee-bot-runner-secret --from-dotenv .env.modal
```

部署 Modal app：

```bash
modal deploy modal_bot_app.py
```

## 3. API 创建 Bot

保持现有 `/api/v1/bots` 接口不变，只需把服务端环境变量 `LAUNCH_BOT_METHOD=modal`。

`external_media_storage_settings` 现在支持 `recording_upload_uri`，可直接传完整输出路径，例如 `r2://bucket/path/file.mp4` 或 `s3://bucket/path/file.mp4`。

示例：

```bash
curl -X POST http://localhost:8000/api/v1/bots \
  -H 'Authorization: Token <YOUR_API_KEY>' \
  -H 'Content-Type: application/json' \
  -d '{
    "meeting_url": "https://meet.google.com/abc-defg-hij",
    "bot_name": "Demo Recorder",
    "recording_settings": {
      "format": "mp4"
    },
    "automatic_leave_settings": {
      "max_uptime_seconds": 10800
    },
    "external_media_storage_settings": {
      "recording_upload_uri": "r2://voxella-video/attendee/demo-run.mp4"
    },
    "metadata": {
      "source": "curl-demo"
    }
  }'
```

## 4. 运行时参数

Modal function `run_bot_on_modal` 支持这些参数：

- `bot_id`
- `bot_name`
- `meeting_url`
- `recording_upload_uri`
- `other_params`

其中 `other_params` 当前主要承载：

- `recording_format`
- `recording_resolution`
- `max_uptime_seconds`

服务端在 `launch_bot()` 时会把这些值一并传给 Modal，用于运行前校正 bot 配置与外部存储凭证。

## 5. 实验命令

为验证完整 launch 流程，新增实验命令：

```bash
python manage.py launch_modal_bot_experiment \
  --project-object-id <project_object_id> \
  --meeting-url '<meeting_url>' \
  --bot-name 'Modal Recorder' \
  --recording-upload-uri 'r2://voxella-video/attendee/manual-test.mp4'
```

只看生成 payload 不实际发起：

```bash
python manage.py launch_modal_bot_experiment \
  --project-object-id <project_object_id> \
  --meeting-url '<meeting_url>' \
  --recording-upload-uri 'r2://voxella-video/attendee/manual-test.mp4' \
  --dry-run
```

## 6. 说明

- 主录制 / cleanup / 上传链路未改，仍走现有 `BotController`
- Modal 仅替换“谁来执行 bot”
- heartbeat timeout / never launched 清理已增加 Modal cancel 分支
