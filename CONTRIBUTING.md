# Contributing

感谢参与 AgentENV Python。

## 本地检查

项目要求 Python 3.10+，运行时没有第三方依赖。

```bash
make test
make demo
```

提交前请确保：

- 新行为有对应测试；
- 生命周期状态变更由 `Orchestrator` 负责；
- 运行时相关逻辑放在 `SandboxBackend` 实现中；
- 不要把 `.agentenv/`、虚拟环境或生成的缓存提交到仓库；
- 文档中的命令可以从仓库根目录直接执行。

## 设计边界

当前 `LocalProcessBackend` 用于表达流程，不是安全沙箱。新增 Docker、
Firecracker 或远程运行时支持时，应实现 `SandboxBackend`，避免把底层细节
泄漏到 API 和编排层。
