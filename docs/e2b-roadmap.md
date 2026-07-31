# E2B 兼容层推进路线

本文维护 AgentENV Python 的 E2B 兼容层实施顺序。目标不是一次性复制 E2B
的全部能力，而是始终先完成一条可运行、可验证的主流程，再逐步扩充协议和
平台能力。

## 维护规则

1. 本文是一个按优先级排列的工作队列，最上面的实施项就是当前阶段。
2. 只有代码、测试和必要文档都达到该项验收标准时，才视为完成。
3. 当前第一项完成后，直接删除该实施项，并重新编号；不要保留“已完成”
   占位。完成历史由 Git 提交记录保存。
4. 如果只完成部分内容，保留该项，并在验收标准中删除已经完成的子项。
5. 新需求默认放在队列末尾；只有阻塞当前主流程时，才允许提高优先级。

## 当前实施队列

### 1. Commands 流式执行和后台进程

在现有同步 `exec` 之外增加进程生命周期管理。

范围：

- 启动前台或后台命令；
- 流式读取 stdout 和 stderr；
- 查询正在运行和已经结束的命令；
- 按 PID 或命令 ID 重新连接；
- 写入 stdin；
- 发送终止信号；
- 保存退出码和必要的执行元数据。

验收标准：

- 同步命令保持向后兼容；
- 长时间命令不会阻塞整个 API 服务；
- 客户端断开后，后台命令可以继续运行并重新连接；
- pause、resume 和 delete 对后台进程的行为有明确测试。

### 2. PTY WebSocket

提供可交互终端数据面。

范围：

- 创建 PTY；
- 双向输入和实时输出；
- 调整终端行列尺寸；
- disconnect、reconnect、wait 和 kill；
- PTY 会话与 Sandbox 生命周期关联。

验收标准：

- 能通过浏览器或示例客户端打开交互式 shell；
- 支持 ANSI 输出、窗口 resize 和重新连接；
- 删除 Sandbox 时不会残留 PTY 进程。

### 3. Sandbox 安全访问

补齐 E2B secure sandbox 的核心认证语义。

范围：

- 控制面 `X-API-Key` 认证；
- 为每个 Sandbox 签发独立 `envdAccessToken`；
- controller 请求校验 `X-Access-Token`；
- token 只以安全形式持久化；
- token 过期、轮换和撤销；
- 审计认证失败事件。

验收标准：

- secure sandbox 不带 token 时无法访问 Commands、Filesystem 和 PTY；
- 非 secure 模式只能通过显式配置开启；
- API 响应不再固定返回空的 `envdAccessToken`；
- 日志和错误响应不会泄露 token。

### 4. 端口流量代理

让 Sandbox 内启动的 HTTP/WebSocket 服务能够从外部访问。

范围：

- `get_host(port)` 风格的主机地址；
- Sandbox ID 和端口到运行时的路由；
- HTTP 与 WebSocket 反向代理；
- `trafficAccessToken`；
- `allowPublicTraffic`；
- 连接触发 auto-resume；
- 端口和请求超时限制。

验收标准：

- Docker Sandbox 内启动的 HTTP 服务可通过代理访问；
- WebSocket 可以持续双向通信；
- 私有流量必须携带有效 token；
- Sandbox pause、resume、delete 后路由状态正确。

### 5. 官方 E2B SDK 端到端兼容测试

使用真实 Python E2B SDK 验证控制面和数据面，而不再只依赖 Fake SDK。

范围：

- 固定并记录经过验证的 E2B SDK 版本；
- 创建、连接、命令、文件、PTY、暂停、恢复和删除测试；
- 对请求字段、响应字段、状态码和错误类型做契约验证；
- 测试失败时也必须清理远端或本地 Sandbox；
- 在 CI 中区分无密钥契约测试和需要密钥的云端冒烟测试。

验收标准：

- 官方 SDK 示例无需修改核心调用方式即可运行；
- CI 能检测 SDK 升级造成的接口不兼容；
- 文档明确列出已兼容和暂未兼容的 SDK 能力。

### 6. Template Builder

增加可构建、可版本化的模板流程。

范围：

- Python Template DSL 和 Dockerfile 导入；
- 安装软件包、复制文件、执行构建命令；
- 环境变量、工作目录和运行用户；
- 同步构建、后台构建、状态和日志；
- 构建缓存、alias 和版本；
- start command、ready command 和就绪快照。

验收标准：

- 可以构建一个预装 Python 依赖的模板；
- 可以构建并快照一个已启动 HTTP 服务的模板；
- 从模板创建 Sandbox 后能够立即执行主流程。

### 7. MCP 与持久卷

补充面向 Agent 工作负载的组合能力。

范围：

- 创建 Sandbox 时声明 MCP Server；
- MCP 配置、密钥注入和健康检查；
- Volume 创建、挂载、查询和删除；
- 只读与读写挂载；
- Local、Docker 和 E2B Volume 的统一抽象；
- Volume 配额和快照。

验收标准：

- 示例 Agent 可以使用 Sandbox 内的 MCP Server；
- 两个 Sandbox 可以按权限共享一个持久卷；
- 删除 Sandbox 不会误删仍被引用的 Volume。

### 8. 平台级可靠性

在核心兼容流程稳定后补充生产化能力。

范围：

- 数据库存储替换单进程 JSON 存储；
- 幂等请求、分页和标准错误结构；
- 多节点调度、容量管理和暖池；
- 资源指标、结构化日志和 OpenTelemetry；
- 租户、配额、审计和 Secret 管理；
- Docker 细粒度出口网络策略；
- 完整 rootfs、进程或内存快照能力。

验收标准：

- 多进程或多节点并发操作不会破坏元数据；
- 关键生命周期操作可观测、可审计、可恢复；
- 安全策略由运行时强制执行，而不是只保存为元数据。
