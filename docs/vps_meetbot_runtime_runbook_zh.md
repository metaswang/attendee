# VPS Meetbot Runtime 运行流程（myvps/myvps2/myvps3）

本文说明当前仓库下，`LAUNCH_BOT_METHOD=hybrid` 且 `MEETBOT_SCHEDULER_SKIP_VPS=false` 时，Meetbot Runtime 在 VPS 上的预期运行方式与落地步骤。

## 一句话结论

VPS 不是直接跑 Django 控制面；VPS 侧应以 **systemd 常驻进程**运行 `attendee-runtime-agent`，从 Redis 队列消费 `launch/stop` 指令，并由 `attendee-bot-runner` 拉起/停止实际 bot 容器。

---

## 1. 架构与职责

- 控制面（attendee app/worker/scheduler）负责：
  - 分配 VPS slot（`myvps -> myvps3 -> myvps2`）
  - 把 launch payload 写入 `meetbot:runtime:commands:{host_name}`
  - 更新 lease/slot 元数据，接收 runtime 完成回调
- VPS（runtime host）负责：
  - `attendee-runtime-agent` 常驻监听 Redis 队列
  - 收到 `launch` 后写入运行时 env，异步执行 `attendee-bot-runner`
  - `attendee-bot-runner` 用 `docker run` 启动 `BOT_RUNTIME_IMAGE` 容器执行 `manage.py run_bot`
  - 结束后回调控制面 `/internal/attendee-runtime-leases/<lease_id>/complete`

---

## 2. 控制面必须配置

控制面环境变量（示例可参考 `deploy/production/myvps.env.example`）最少需要：

- `LAUNCH_BOT_METHOD=hybrid`
- `MEETBOT_RUNTIME_SCHEDULER_ENABLED=true`
- `MEETBOT_SCHEDULER_SKIP_VPS=false`
- `MEETBOT_VPS_TARGET_ORDER=myvps,myvps3,myvps2`
- `MEETBOT_VPS_SLOT_CAPACITY_JSON={"myvps":2,"myvps3":1,"myvps2":1}`（按实际调整）
- `BOT_RUNTIME_IMAGE=<可在 VPS 拉取的镜像>`
- `BOT_RUNTIME_REDIS_URL` 或 `REDIS__URL/REDIS_URL`
- `MEETBOT_RUNTIME_API_BASE_URL=<runtime 可访问的控制面地址>`
- `ATTENDEE_INTERNAL_SERVICE_KEY=<与 voxella-api 对齐>`

---

## 3. 每台 VPS 的预期目录与组件

每台 runtime VPS（如 `myvps/myvps2/myvps3`）建议具备：

- 目录
  - `/voxella/attendee`（代码目录，或你在 env 中指定的 `ATTENDEE_REPO_DIR`）
  - `/etc/attendee`
  - `/var/log/attendee`
- 可执行文件
  - `/usr/local/bin/attendee-runtime-agent`（来自 `scripts/runtime_agent.py`）
  - `/usr/local/bin/attendee-bot-runner`（来自 `scripts/digitalocean/attendee-bot-runner.sh`）
- systemd unit
  - `attendee-runtime-agent.service`
  - `attendee-bot-runner.service`（单次 runner 用，通常不需要 enable）

---

## 4. VPS 落地步骤（单机）

以下步骤在每台 VPS 执行一次。

### 4.1 同步代码到 VPS

按项目约定，先在本机更新后同步：

```bash
rsync -avr /Users/adamwang/Project/subdub/voxella-attendee/ myvps2:/voxstudio/attendee/
```

然后登录 VPS（示例）：

```bash
ssh myvps2
cd /voxstudio/attendee
```

### 4.2 安装运行脚本与 service

```bash
sudo install -D -m 0755 scripts/runtime_agent.py /usr/local/bin/attendee-runtime-agent
sudo install -D -m 0755 scripts/digitalocean/attendee-bot-runner.sh /usr/local/bin/attendee-bot-runner
sudo install -D -m 0644 scripts/digitalocean/attendee-runtime-agent.service /etc/systemd/system/attendee-runtime-agent.service
sudo install -D -m 0644 scripts/digitalocean/attendee-bot-runner.service /etc/systemd/system/attendee-bot-runner.service
sudo mkdir -p /etc/attendee /var/log/attendee
```

### 4.3 写入 agent 环境文件

创建 `/etc/attendee/runtime-agent.env`（关键变量）：

```bash
sudo tee /etc/attendee/runtime-agent.env >/dev/null <<'EOF'
MEETBOT_RUNTIME_HOST_NAME=myvps2
MEETBOT_RUNTIME_QUEUE_KEY=meetbot:runtime:commands:myvps2
REDIS_URL=rediss://:<password>@<redis-host>:6379/5
ATTENDEE_REPO_DIR=/voxstudio/attendee
ATTENDEE_CONTAINER_WORKDIR=/attendee
BOT_RUNTIME_IMAGE=attendee-bot-runner:latest
LOG_LEVEL=INFO
EOF
```

说明：

- `MEETBOT_RUNTIME_HOST_NAME` 必须与控制面 `MEETBOT_VPS_TARGET_ORDER` 中名称一致。
- `MEETBOT_RUNTIME_QUEUE_KEY` 建议与默认模板保持一致：`meetbot:runtime:commands:{host_name}`。
- 每机 Docker 资源上限（覆盖 scheduler 下发的 `BOT_MEMORY_*` / `BOT_CPUS`，由 `runtime_agent` 在写 `runtime.env` 前合并）：`MEETBOT_RUNTIME_HOST_BOT_CPUS`（如 `2` 或 `1.5`）、`MEETBOT_RUNTIME_HOST_BOT_MEMORY_LIMIT`（如 `2g`）。可选 `MEETBOT_RUNTIME_HOST_BOT_MEMORY_RESERVATION`；若不设则与 limit 相同。

### 4.4 启动并设置开机自启

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now attendee-runtime-agent.service
sudo systemctl status attendee-runtime-agent.service --no-pager
```

---

## 5. 运行时链路（实际发生什么）

1. scheduler 调用 `launch_meetbot_runtime`，优先尝试 `vps_docker` provider。  
2. `acquire_vps_slot` 在 Redis 中预占 slot（带 TTL）。  
3. provider 把 launch payload 推入 `meetbot:runtime:commands:{host_name}`。  
4. VPS 上 `attendee-runtime-agent` `BLPOP` 到 payload：  
   - `launch`：写 `/etc/attendee/runtime.env`，启动 `attendee-bot-runner`  
   - `stop`：执行 `docker rm -f <container_name>`  
5. `attendee-bot-runner` 使用 `docker run` 启动 bot 容器，执行 `python manage.py run_bot`。  
6. bot 结束后 runner 回调 lease complete 接口，控制面释放 slot/更新状态。  

---

## 6. 验证清单

### 6.1 控制面侧

- 查看 runtime target snapshot（或相关 API）确认 VPS target 有可用 slot。
- 发起一个 bot 后，确认 lease provider 为 `vps_docker`（未回退到 GCP）。

### 6.2 VPS 侧

```bash
sudo systemctl status attendee-runtime-agent.service --no-pager
sudo journalctl -u attendee-runtime-agent.service -n 100 --no-pager
sudo tail -n 100 /var/log/attendee/runner.log
sudo docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Image}}'
```

观察点：

- 心跳 key 持续刷新：`meetbot:runtime:agent:<host>:heartbeat`
- 队列消费后出现新容器（名称前缀通常为 `attendee-bot-*`）
- runner 日志里能看到 callback 成功或至少请求发出

---

## 7. 常见问题

- `MEETBOT_SCHEDULER_SKIP_VPS=true`：会直接跳过 VPS 分配，全部走 GCP。
- `host_name` 不匹配：控制面投递到了 `myvps2` 队列，但 VPS 上 agent 配成 `myvps`，会导致“看起来没消费”。
- `BOT_RUNTIME_IMAGE` 不可拉取：runner 启动失败，检查镜像仓库权限和网络。
- Redis 不通或 TLS 配置错误：agent 无法 `BLPOP`，journal 会出现 redis-cli 错误。
- 回调地址不可达：bot 能跑但 lease 回收异常，需检查 `MEETBOT_RUNTIME_API_BASE_URL` 与网络连通。

---

## 8. 推荐运维动作

- 新增/替换 VPS 后，先做“空跑验证”（手工 push 一条测试 launch payload）再放量。
- 保持控制面和 VPS 上 runner/agent 脚本版本同步（每次发布后 rsync + reload service）。
- 每台 VPS 固定唯一 host_name，不要复用同名节点，避免 slot 与队列冲突。

