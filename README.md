# AgentENV Python

用 Python 表达 AgentENV 核心设计的轻量实现，覆盖完整沙箱生命周期：

```text
OCI/Template → Sandbox → Exec → Pause/Resume
             → Snapshot/Restore/Fork → Delete
```

项目采用“编排器 + 可替换后端 + 元数据持久化 + HTTP/CLI”的结构。可以使用
本机进程后端快速理解流程，也可以使用 Docker 后端获得容器隔离、OCI 镜像、
资源限制和真实的 pause/resume。

> [!WARNING]
> `LocalProcessBackend` 不是安全沙箱。Docker 后端也不能替代经过加固的
> 多租户隔离方案。不要把未配置认证的服务暴露到公网。

## 已实现

- Local、Docker 与 E2B 三种运行时后端；
- Docker Hub、私有 Registry、tag 和 digest 形式的 OCI 引用解析；
- OCI 镜像检查，缺失时可自动 `docker pull`；
- CPU、内存、PID 限制以及磁盘配额元数据；
- 完全断网/恢复联网和 allow/deny 网络策略持久化；
- 模板、沙箱、命令、暂停、恢复、快照、恢复、分叉和删除；
- TTL kill/pause、auto-resume、手动清理和服务端后台维护；
- JSON 原子持久化、中断操作恢复和生命周期审计事件；
- E2B 风格的控制面字段、状态码和生命周期接口；
- Python 3.10–3.12 自动化测试。

## 本机后端快速开始

需要 Python 3.10+，运行时无第三方 Python 依赖。

```bash
make demo
make test
```

等价命令：

```bash
PYTHONPATH=src python3.10 -m agentenv \
  --data-dir /tmp/agentenv-py-demo demo
```

## Docker 后端

先确保 Docker Desktop 或 Docker Engine 正在运行：

```bash
docker info
```

创建 OCI 模板和受限沙箱。全局参数 `--backend docker` 要放在子命令前：

```bash
export PYTHONPATH=src

python3.10 -m agentenv --backend docker \
  template-create alpine --source alpine:3.20

python3.10 -m agentenv --backend docker start alpine \
  --timeout 600 \
  --cpus 1.5 \
  --memory-mb 256 \
  --pids-limit 128 \
  --disk-mb 1024 \
  --no-internet \
  --on-timeout pause \
  --auto-resume
```

也可以绕过模板，直接冷启动一个 OCI 镜像：

```bash
python3.10 -m agentenv --backend docker \
  cold-start ghcr.io/example/agent:latest --memory-mb 512
```

运行与动态更新：

```bash
python3.10 -m agentenv --backend docker exec <sandbox-id> \
  'cat /etc/os-release && echo hello > hello.txt'

python3.10 -m agentenv --backend docker resources <sandbox-id> \
  --cpus 2 --memory-mb 512 --pids-limit 256

python3.10 -m agentenv --backend docker network <sandbox-id> --no-internet
python3.10 -m agentenv --backend docker network <sandbox-id> --internet

python3.10 -m agentenv --backend docker pause <sandbox-id>
python3.10 -m agentenv --backend docker resume <sandbox-id>
python3.10 -m agentenv --backend docker snapshot <sandbox-id>
```

Docker 后端把 `/workspace` 绑定到宿主机沙箱目录。快照会复制该工作区，不会
捕获容器镜像层中 `/workspace` 之外的改动。`disk_size_mb` 当前作为调度和
接口元数据保存，因为 Docker bind mount 没有跨平台的目录配额能力。

### 网络策略边界

`allow_internet_access=false` 或 `deny_out=["0.0.0.0/0"]` 会真实断开容器
网络；恢复联网会重新连接 Docker `bridge` 网络。`allow_out` 和更细粒度的
`deny_out` 会经过格式验证、持久化并出现在 API 中，但 Docker CLI 后端暂不
使用 iptables/eBPF 强制逐条执行。`SandboxBackend.update_network` 已提供
独立边界，可以继续接入平台级网络执行器。

## HTTP 与 E2B 控制面兼容

启动本机或 Docker API：

```bash
PYTHONPATH=src python3.10 -m agentenv \
  --backend docker \
  --data-dir /tmp/agentenv-docker \
  serve
```

E2B 风格模板启动：

```bash
curl -X POST http://127.0.0.1:8000/sandboxes \
  -H 'content-type: application/json' \
  -d '{
    "templateID": "alpine",
    "timeout": 600,
    "envVars": {"NAME": "AgentENV"},
    "cpuCount": 1,
    "memoryMB": 256,
    "lifecycle": {"onTimeout": "pause", "autoResume": true},
    "allow_internet_access": false
  }'
```

冷启动与生命周期：

```bash
curl -X POST http://127.0.0.1:8000/sandboxes-cold \
  -H 'content-type: application/json' \
  -d '{"image":"alpine:3.20","timeout":300}'

curl -X POST http://127.0.0.1:8000/sandboxes/<id>/pause
curl -X POST http://127.0.0.1:8000/sandboxes/<id>/connect \
  -H 'content-type: application/json' -d '{"timeout":300}'
curl -X POST http://127.0.0.1:8000/sandboxes/<id>/timeout \
  -H 'content-type: application/json' -d '{"timeout":600}'
curl http://127.0.0.1:8000/v2/sandboxes
```

已兼容的是 E2B 控制面生命周期：create/list/get/kill、pause/connect、
timeout、fork 和 cold start，以及 `sandboxID/templateID/startedAt/endAt`
等响应字段。官方 SDK 的 `commands`、`files`、PTY 和端口代理会连接沙箱内
controller/envd 的独立 ConnectRPC/WebSocket 接口；本项目目前只提供
`POST /sandboxes/{id}/exec` 扩展，因此尚不能宣称完整 SDK 零改动兼容。

## E2B 后端（托管沙箱）

除了本机和 Docker，还可以把沙箱跑在 [E2B](https://e2b.dev) 的托管运行时上。
E2B 后端是一个可选依赖：

```bash
pip install -e ".[e2b]"
```

在 `.env` 里配置 API Key（与官方 SDK 一致）：

```text
E2B_API_KEY=...
```

然后用 `--backend e2b` 启动，命令与其它后端完全一致：

```bash
PYTHONPATH=src python3.10 -m agentenv --backend e2b \
  --data-dir /tmp/agentenv-e2b template-create base --source base

PYTHONPATH=src python3.10 -m agentenv --backend e2b --data-dir /tmp/agentenv-e2b \
  start base --timeout 300 --env MESSAGE=hello --on-timeout pause --auto-resume

PYTHONPATH=src python3.10 -m agentenv --backend e2b --data-dir /tmp/agentenv-e2b \
  exec <sandbox-id> 'echo "$MESSAGE" > result.txt && cat result.txt'

PYTHONPATH=src python3.10 -m agentenv --backend e2b --data-dir /tmp/agentenv-e2b \
  pause <sandbox-id>
PYTHONPATH=src python3.10 -m agentenv --backend e2b --data-dir /tmp/agentenv-e2b \
  resume <sandbox-id>
PYTHONPATH=src python3.10 -m agentenv --backend e2b --data-dir /tmp/agentenv-e2b \
  snapshot <sandbox-id>
```

E2B 后端的映射关系：

| 本项目操作 | E2B SDK 调用 |
|---|---|
| 创建沙箱 | `Sandbox.create(template, timeout, envs, metadata, allow_internet_access, network, lifecycle)` |
| 执行命令 | `Sandbox.connect(sandbox_id)` → `commands.run(cmd, envs, cwd, timeout)` |
| 暂停 / 恢复 | `Sandbox.pause(sandbox_id)` / `Sandbox.connect(sandbox_id)` |
| 快照 / 恢复 | `Sandbox.create_snapshot(sandbox_id)` → `Sandbox.create(template=snapshot_id)` |
| 删除沙箱 | `Sandbox.kill(sandbox_id)` |
| 更新网络 | `Sandbox.update_network(sandbox_id, network)` |
| 存活检测 | `Sandbox.get_info(sandbox_id)` |

E2B 的 `sandbox_id` 存在 `Sandbox.runtime_id` 里，所以句柄可以跨操作和进程
重启重建。资源限制（CPU/内存）由模板固定，运行时不可变，`update_resources`
是空操作但仍会持久化为元数据。E2B 快照是云端的，其 ID 记录在快照目录下的
`e2b_snapshot.json` 中，删除快照时会同步删除云端快照。冷启动（OCI 镜像）
是 Docker 专属概念，E2B 用模板启动。

端到端示例（真实调用 E2B API）见
[`examples/e2b_backend_demo.py`](examples/e2b_backend_demo.py)。

相关官方说明：

- [E2B sandbox lifecycle](https://e2b.dev/docs/sandbox)
- [E2B persistence and connect](https://e2b.dev/docs/sandbox/persistence)
- [E2B sandbox controller](https://e2b.dev/docs/sandbox/secured-access)

完整接口契约见 [docs/openapi.yaml](docs/openapi.yaml)。

从启动服务到 Sandbox 创建、命令执行、暂停恢复、资源网络策略、快照分叉和
Python 客户端的完整调用示例，见
[E2B 兼容 API 调用指南](docs/e2b-api-guide.md)。

## 项目结构

```text
src/agentenv/
├── models.py        # 模板、资源、网络、沙箱、快照和事件
├── oci.py           # OCI 引用解析与 Docker 镜像解析
├── backend.py       # LocalProcessBackend / DockerSandboxBackend（抽象 + 两种实现）
├── e2b_backend.py   # E2BSandboxBackend（可选依赖 e2b）
├── orchestrator.py  # 状态机、回滚、恢复、TTL 和策略更新
├── e2b.py           # E2B 请求/响应适配
├── store.py         # JSON 元数据原子持久化
├── api.py           # HTTP API 与后台维护
└── cli.py           # CLI 与端到端 Demo
```

设计说明见 [docs/architecture.md](docs/architecture.md)。

## 仍然刻意简化的部分

- Docker 快照只包含绑定工作区，不包含内存和完整容器 rootfs；
- Local 后端暂停/恢复只是逻辑状态转换；
- 没有 API Key、租户授权和公网流量代理；
- 细粒度网络列表尚未接入 iptables/eBPF；
- JSON 存储适合单进程演示，不适合分布式并发；
- 完整 E2B SDK 兼容还需要沙箱 controller/envd 层。

## 许可证

[MIT](LICENSE)
