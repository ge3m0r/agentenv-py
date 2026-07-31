# E2B 兼容 API 调用指南

本文说明如何通过 HTTP 调用 AgentENV Python 提供的 E2B 兼容控制面。

当前兼容范围：

- Sandbox 创建、查询、列表和删除；
- pause、connect/resume、timeout；
- cold start、snapshot 和 fork；
- CPU、内存、PID 和网络策略；
- E2B 风格的 camelCase 请求与响应字段；
- 生命周期事件；
- AgentENV 扩展的同步命令执行和 Local/Docker Filesystem 接口。

当前尚未实现 Sandbox Controller/envd，因此官方 E2B SDK 的 `files`、PTY、
流式 commands 和端口代理还不能零修改接入。命令和文件操作暂时使用
`POST /sandboxes/{sandboxID}/exec` 与 `/files/*` 扩展接口。

> 当前服务没有 API Key 认证，只应监听本机或可信网络。

## 1. 启动服务

### 本机流程后端

本机后端适合快速验证接口。命令会使用服务进程当前用户权限执行，不是安全
沙箱。

```bash
PYTHONPATH=src python3.10 -m agentenv \
  --backend local \
  --data-dir /tmp/agentenv-api \
  serve
```

### Docker 后端

先启动 Docker Desktop/Engine，然后执行：

```bash
PYTHONPATH=src python3.10 -m agentenv \
  --backend docker \
  --data-dir /tmp/agentenv-docker-api \
  serve
```

### E2B 后端（托管沙箱）

把沙箱跑在 E2B 云上需要可选依赖和 API Key：

```bash
pip install -e ".[e2b]"     # 安装 e2b SDK
echo 'E2B_API_KEY=...' > .env
PYTHONPATH=src python3.10 -m agentenv \
  --backend e2b \
  --data-dir /tmp/agentenv-e2b-api \
  serve
```

E2B 后端的模板用 E2B 模板名（默认 `base`），不是 OCI 镜像。资源限制由模板
固定、运行时不可变；冷启动（OCI 镜像）不可用。详见 README 的「E2B 后端」一节。

默认地址：

```text
http://127.0.0.1:8000
```

检查服务：

```bash
curl http://127.0.0.1:8000/health
```

## 2. 创建模板

模板管理是 AgentENV 扩展接口。Docker 后端会解析并检查 `source` 中的 OCI
镜像；镜像不存在时默认自动拉取。

```bash
curl -X POST http://127.0.0.1:8000/templates \
  -H 'content-type: application/json' \
  -d '{
    "name": "alpine",
    "source": "alpine:3.20",
    "env": {"APP_ENV": "development"},
    "workdir": "."
  }'
```

列出模板：

```bash
curl http://127.0.0.1:8000/templates
```

本机后端可以使用 `"source":"scratch"`。

## 3. 从模板创建 Sandbox

E2B 风格请求使用 `templateID`、`envVars`、`cpuCount` 等 camelCase 字段：

```bash
curl -X POST http://127.0.0.1:8000/sandboxes \
  -H 'content-type: application/json' \
  -d '{
    "templateID": "alpine",
    "timeout": 600,
    "envVars": {
      "NAME": "AgentENV",
      "MODE": "demo"
    },
    "metadata": {
      "user": "demo-user",
      "task": "e2b-api-guide"
    },
    "cpuCount": 1,
    "memoryMB": 256,
    "diskSizeMB": 1024,
    "pidsLimit": 128,
    "allow_internet_access": false,
    "lifecycle": {
      "onTimeout": "pause",
      "autoResume": true
    }
  }'
```

响应示例：

```json
{
  "sandboxID": "sbx_19b42f81d6aa",
  "clientID": "sbx_19b42f81d6aa",
  "templateID": "tpl_a071532be192",
  "startedAt": "2026-07-31T00:00:00+00:00",
  "endAt": "2026-07-31T00:10:00+00:00",
  "state": "running",
  "cpuCount": 1,
  "memoryMB": 256,
  "diskSizeMB": 1024,
  "envdVersion": "agentenv-py",
  "metadata": {
    "user": "demo-user",
    "task": "e2b-api-guide"
  },
  "allowInternetAccess": false,
  "network": {
    "allowOut": [],
    "denyOut": []
  },
  "backend": "docker",
  "imageRef": "docker.io/library/alpine:3.20",
  "lifecycle": {
    "onTimeout": "pause",
    "autoResume": true
  }
}
```

保存响应中的 `sandboxID`，后续接口都使用这个 ID。

### 生命周期参数

| 字段 | 值 | 行为 |
|---|---|---|
| `timeout` | 秒 | 从创建时刻开始计算 TTL |
| `onTimeout` | `kill` | 到期后删除 Sandbox |
| `onTimeout` | `pause` | 到期后暂停并保留状态 |
| `autoResume` | `true` | 暂停后收到命令时自动恢复 |

`autoResume=true` 只能与 `onTimeout=pause` 一起使用。

## 4. 直接从 OCI 镜像冷启动

Docker 后端支持跳过显式模板创建：

```bash
curl -X POST http://127.0.0.1:8000/sandboxes-cold \
  -H 'content-type: application/json' \
  -d '{
    "image": "alpine:3.20",
    "timeout": 300,
    "cpuCount": 1,
    "memoryMB": 256,
    "envVars": {"SOURCE": "cold-start"}
  }'
```

支持以下 OCI 形式：

```text
ubuntu:22.04
ghcr.io/acme/agent:latest
registry.example.com/team/image:v1
registry.example.com/team/image@sha256:<digest>
```

## 5. 执行命令

这是 AgentENV 扩展接口，不是完整 envd Commands API。

```bash
curl -X POST \
  http://127.0.0.1:8000/sandboxes/<sandboxID>/exec \
  -H 'content-type: application/json' \
  -d '{
    "command": "echo \"$NAME\" > result.txt && cat result.txt",
    "timeout": 30
  }'
```

响应：

```json
{
  "command": "echo \"$NAME\" > result.txt && cat result.txt",
  "exit_code": 0,
  "stdout": "AgentENV\n",
  "stderr": "",
  "duration_ms": 8,
  "executed_at": "2026-07-31T00:00:10+00:00"
}
```

命令超时使用退出码 `124`。

## 6. Filesystem 文件操作

Filesystem 接口以 Sandbox 工作区作为虚拟根目录。绝对路径 `/data/a.txt` 和
相对路径 `data/a.txt` 都会定位到工作区内部；包含 `..` 或通过符号链接逃逸
工作区的路径会被拒绝。

创建目录并写入 UTF-8 文本：

```bash
curl -X POST \
  http://127.0.0.1:8000/sandboxes/<sandboxID>/files/mkdir \
  -H 'content-type: application/json' \
  -d '{"path":"/data/incoming"}'

curl -X POST \
  http://127.0.0.1:8000/sandboxes/<sandboxID>/files/write \
  -H 'content-type: application/json' \
  -d '{
    "path": "/data/incoming/message.txt",
    "data": "hello from filesystem",
    "encoding": "utf-8"
  }'
```

读取和列出目录：

```bash
curl -X POST \
  http://127.0.0.1:8000/sandboxes/<sandboxID>/files/read \
  -H 'content-type: application/json' \
  -d '{"path":"/data/incoming/message.txt","encoding":"utf-8"}'

curl -X POST \
  http://127.0.0.1:8000/sandboxes/<sandboxID>/files/list \
  -H 'content-type: application/json' \
  -d '{"path":"/data/incoming"}'
```

二进制数据使用 Base64：

```bash
curl -X POST \
  http://127.0.0.1:8000/sandboxes/<sandboxID>/files/write \
  -H 'content-type: application/json' \
  -d '{
    "path": "/data/payload.bin",
    "data": "AP9hZ2VudGVudg==",
    "encoding": "base64"
  }'
```

查询、移动和删除：

```bash
curl -X POST \
  http://127.0.0.1:8000/sandboxes/<sandboxID>/files/stat \
  -H 'content-type: application/json' \
  -d '{"path":"/data/incoming/message.txt"}'

curl -X POST \
  http://127.0.0.1:8000/sandboxes/<sandboxID>/files/move \
  -H 'content-type: application/json' \
  -d '{
    "source": "/data/incoming/message.txt",
    "destination": "/data/message.txt"
  }'

curl -i -X POST \
  http://127.0.0.1:8000/sandboxes/<sandboxID>/files/remove \
  -H 'content-type: application/json' \
  -d '{"path":"/data","recursive":true}'
```

Local 和 Docker 后端使用同一 Filesystem 契约；Docker 的工作区对应容器内
`/workspace`。当前这些是 AgentENV HTTP 扩展接口，尚不是 E2B envd 的
ConnectRPC Filesystem 协议。

## 7. 查询 Sandbox

查询一个 Sandbox：

```bash
curl http://127.0.0.1:8000/sandboxes/<sandboxID>
```

列表：

```bash
curl http://127.0.0.1:8000/v2/sandboxes
```

按状态过滤：

```bash
curl 'http://127.0.0.1:8000/v2/sandboxes?state=running'
curl 'http://127.0.0.1:8000/v2/sandboxes?state=paused'
```

按 metadata 过滤时，需要对整个 metadata 表达式进行 URL 编码：

```bash
curl --get http://127.0.0.1:8000/v2/sandboxes \
  --data-urlencode 'metadata=user=demo-user&task=e2b-api-guide'
```

## 8. 暂停与恢复

暂停：

```bash
curl -i -X POST \
  http://127.0.0.1:8000/sandboxes/<sandboxID>/pause
```

成功返回 `204 No Content`。

使用 E2B 风格的 connect 恢复，并从当前时刻重新设置 TTL：

```bash
curl -X POST \
  http://127.0.0.1:8000/sandboxes/<sandboxID>/connect \
  -H 'content-type: application/json' \
  -d '{"timeout":600}'
```

- 已经 running：返回 `200`；
- 从 paused 恢复：返回 `201`。

也可以调用：

```bash
curl -X POST \
  http://127.0.0.1:8000/sandboxes/<sandboxID>/resume \
  -H 'content-type: application/json' \
  -d '{"timeout":600}'
```

## 9. 更新 TTL

```bash
curl -i -X POST \
  http://127.0.0.1:8000/sandboxes/<sandboxID>/timeout \
  -H 'content-type: application/json' \
  -d '{"timeout":1200}'
```

成功返回 `204`。新 TTL 从请求发生时重新计算。

使用 `0` 或 `null` 可以清除 TTL：

```bash
curl -X POST \
  http://127.0.0.1:8000/sandboxes/<sandboxID>/timeout \
  -H 'content-type: application/json' \
  -d '{"timeout":0}'
```

## 10. 更新资源限制

Docker 后端会通过 `docker update` 更新 CPU、内存和 PID 限制：

```bash
curl -X PUT \
  http://127.0.0.1:8000/sandboxes/<sandboxID>/resources \
  -H 'content-type: application/json' \
  -d '{
    "cpuCount": 2,
    "memoryMB": 512,
    "diskSizeMB": 2048,
    "pidsLimit": 256
  }'
```

`diskSizeMB` 会保存为调度元数据。Docker bind mount 没有统一的跨平台目录
配额能力，所以当前不会实时限制工作区大小。

## 11. 更新网络策略

完全断网：

```bash
curl -X PUT \
  http://127.0.0.1:8000/sandboxes/<sandboxID>/network \
  -H 'content-type: application/json' \
  -d '{"allowInternetAccess":false}'
```

恢复联网：

```bash
curl -X PUT \
  http://127.0.0.1:8000/sandboxes/<sandboxID>/network \
  -H 'content-type: application/json' \
  -d '{"allowInternetAccess":true}'
```

记录细粒度规则：

```bash
curl -X PUT \
  http://127.0.0.1:8000/sandboxes/<sandboxID>/network \
  -H 'content-type: application/json' \
  -d '{
    "allowInternetAccess": true,
    "allowOut": ["api.example.com", "10.0.0.0/8"],
    "denyOut": ["203.0.113.10/32"]
  }'
```

当前 Docker 后端只强制执行完全断网/恢复联网。细粒度 allow/deny 规则会被
验证和持久化，但还没有接入 iptables/eBPF 执行器。

## 12. Snapshot 与 Fork

创建 Snapshot：

```bash
curl -X POST \
  http://127.0.0.1:8000/sandboxes/<sandboxID>/snapshots
```

从 Snapshot 创建新 Sandbox：

```bash
curl -X POST http://127.0.0.1:8000/sandboxes \
  -H 'content-type: application/json' \
  -d '{"snapshot_id":"<snapshotID>","timeout_seconds":600}'
```

批量 Fork：

```bash
curl -X POST \
  http://127.0.0.1:8000/sandboxes/<sandboxID>/fork \
  -H 'content-type: application/json' \
  -d '{"count":2}'
```

当前 Docker Snapshot 复制 `/workspace`，不包含完整容器 rootfs 和内存。

## 13. 生命周期事件

查询 Sandbox 最近事件：

```bash
curl \
  'http://127.0.0.1:8000/events/sandboxes/<sandboxID>?limit=20'
```

可选参数：

| 参数 | 说明 |
|---|---|
| `limit` | 返回数量，最大 100 |
| `offset` | 跳过数量 |
| `orderAsc` | 是否按时间升序 |
| `types` | 过滤事件类型，可重复传递 |

示例：

```bash
curl --get \
  http://127.0.0.1:8000/events/sandboxes/<sandboxID> \
  --data 'limit=10' \
  --data 'types=command_executed'
```

## 14. 删除 Sandbox

```bash
curl -i -X DELETE \
  http://127.0.0.1:8000/sandboxes/<sandboxID>
```

成功返回 `204 No Content`。Docker 后端会删除容器和对应工作目录。

## 15. Python 完整示例

仓库提供了一个只使用 Python 标准库的完整示例：

```bash
PYTHONPATH=src python3.10 examples/e2b_api_client.py \
  --base-url http://127.0.0.1:8000 \
  --template demo \
  --source scratch
```

Docker 冷启动：

```bash
python3.10 examples/e2b_api_client.py \
  --base-url http://127.0.0.1:8000 \
  --cold-image alpine:3.20
```

该示例会依次：

1. 创建模板或冷启动；
2. 创建 Sandbox；
3. 执行命令；
4. 暂停；
5. 使用 connect 恢复；
6. 查询生命周期事件；
7. 删除 Sandbox。

## 16. 常见错误

| HTTP 状态 | 含义 |
|---|---|
| `400` | 参数错误，例如内存小于 128 MB |
| `404` | Sandbox、Template 或 Snapshot 不存在 |
| `409` | 状态冲突、后端不匹配或资源仍被引用 |
| `500` | Docker daemon、文件系统或未处理的服务错误 |

Docker 常见检查：

```bash
docker info
docker ps -a --filter label=agentenv.sandbox.id
python3.10 -m agentenv --backend docker status
python3.10 -m agentenv --backend docker events --limit 50
```

完整接口结构还可以参考 [OpenAPI](openapi.yaml)。
