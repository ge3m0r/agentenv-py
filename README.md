# AgentENV Python

一个用 Python 表达 AgentENV 核心设计的轻量实现，优先展示完整、可运行的
沙箱生命周期：

```text
Template → Sandbox → Exec → Pause/Resume → Snapshot/Restore/Fork → Delete
```

它参考 AgentENV 的领域划分，保留“编排器 + 可替换后端 + 元数据持久化 +
HTTP/CLI”的结构。当前后端使用工作目录和本机子进程，因此可以在 macOS
和 Linux 上直接运行，不依赖 Firecracker、KVM 或第三方 Python 包。

> [!WARNING]
> `LocalProcessBackend` 不是安全沙箱。命令使用当前用户权限在本机执行，
> 请勿暴露到公网或处理不可信输入。

## 已实现

- 模板创建、查询和安全删除；
- 从模板或快照创建沙箱；
- 命令执行、超时和退出码；
- 暂停、恢复、快照、恢复和批量分叉；
- 沙箱 TTL 更新、手动清理和服务端自动回收；
- JSON 原子持久化和重启后的中断状态协调；
- 生命周期审计事件与运行状态汇总；
- 零依赖 HTTP API 和命令行；
- Python 3.10–3.12 自动化测试。

## 快速开始

需要 Python 3.10 或更高版本。本机默认的 `python3` 可能仍指向 Python
3.6，因此示例显式使用 `python3.10`。

```bash
make demo
```

等价命令：

```bash
PYTHONPATH=src python3.10 -m agentenv \
  --data-dir /tmp/agentenv-py-demo demo
```

测试：

```bash
make test
```

也可以安装为本地命令：

```bash
python3.10 -m pip install -e .
aenv-py --data-dir /tmp/agentenv-py-demo demo
```

## 分步体验

以下命令默认使用仓库下的 `.agentenv/` 保存状态：

```bash
export PYTHONPATH=src

python3.10 -m agentenv template-create ubuntu --source ubuntu:22.04
python3.10 -m agentenv start ubuntu --timeout 600 --env NAME=AgentENV
python3.10 -m agentenv exec <sandbox-id> \
  'echo "hello $NAME" > hello.txt && cat hello.txt'

python3.10 -m agentenv pause <sandbox-id>
python3.10 -m agentenv resume <sandbox-id>
python3.10 -m agentenv snapshot <sandbox-id>
python3.10 -m agentenv restore <snapshot-id>
python3.10 -m agentenv fork <sandbox-id> --count 2

python3.10 -m agentenv timeout <sandbox-id> --seconds 1200
python3.10 -m agentenv status
python3.10 -m agentenv events --limit 20
python3.10 -m agentenv delete <sandbox-id>
```

`--source ubuntu:22.04` 当前记录模板来源，但不会拉取 OCI 镜像。使用
`--base-dir ./some-directory` 可以把一个本机目录作为模板初始文件系统。

## HTTP API

启动服务器：

```bash
make serve
```

服务默认监听 `http://127.0.0.1:8000`。启动后后台维护线程会自动删除 TTL
到期的沙箱。

```bash
curl -X POST http://127.0.0.1:8000/templates \
  -H 'content-type: application/json' \
  -d '{"name":"demo","source":"scratch"}'

curl -X POST http://127.0.0.1:8000/sandboxes \
  -H 'content-type: application/json' \
  -d '{"template_id":"demo","timeout_seconds":600}'

curl -X POST http://127.0.0.1:8000/sandboxes/<sandbox-id>/exec \
  -H 'content-type: application/json' \
  -d '{"command":"printf hello"}'

curl http://127.0.0.1:8000/status
curl 'http://127.0.0.1:8000/events?limit=20'
```

完整接口契约见 [docs/openapi.yaml](docs/openapi.yaml)。

## 项目结构

```text
src/agentenv/
├── models.py        # Template/Sandbox/Snapshot/Event 数据模型
├── store.py         # JSON 元数据原子持久化
├── backend.py       # 可替换运行时接口和本机后端
├── orchestrator.py  # 生命周期状态机、回滚、恢复和 TTL
├── api.py           # HTTP API 与后台维护
├── cli.py           # 命令行和端到端 Demo
└── __main__.py      # python -m agentenv 入口
```

设计说明见 [docs/architecture.md](docs/architecture.md)。

## 与生产级 AgentENV 的差异

本项目表达核心流程，不以性能或生产隔离为目标：

- 暂停/恢复是逻辑状态转换，不保存进程内存；
- 快照复制工作目录，不是增量块设备快照；
- 每次命令执行都是独立子进程；
- 暂无认证、网络策略、CPU/内存限制和多节点调度；
- JSON 存储适用于单进程演示，不适合分布式并发。

下一阶段可以实现 `DockerSandboxBackend`，然后补充 OCI 镜像解析、资源限制、
网络策略和 E2B 兼容层。编排器和 API 无需因底层运行时变化而重写。

## 许可证

[MIT](LICENSE)
