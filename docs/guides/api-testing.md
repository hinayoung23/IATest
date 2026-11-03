# API 自动化

## 导入与管理接口

支持 OpenAPI 3.x、Swagger 2.0、Markdown 和 HTML 文档。接口信息保留方法、路径、参数、请求/响应 Schema 及来源，用于规划和生成；API 文档内容不能自行授予文件、网络或工具权限。

```sh
iatest discover ./api-docs/openapi.yaml --project demo
iatest discover https://example.com/openapi.json --project demo
iatest generate --project demo
```

控制台「接口信息」可导入、编辑和维护接口。接口变化可触发增量维护，缓存不能绕过契约变化和质量门禁。

## 用例与执行

Markdown 是可审阅的 API 用例源文件，包含编号、等级、设计方法、请求、断言、步骤、数据与依赖。生成流程区分接口分析、覆盖规划、场景设计和独立审计。无可信预期、无法编译或依赖不完整的候选不作为可运行用例。

```sh
iatest run --project demo
iatest run --project demo --cases TC-API-0001,TC-API-0002
iatest run --project demo --base-url http://127.0.0.1:9000
```

控制台支持选中、冒烟、全量及定时执行；结果在报告中呈现，修改用例后重新运行门禁。详细字段见[用例格式](../reference/case-format.md)。

## 数据与鉴权

数据集、受约束生成、固定 seed、环境变量引用与失败样本重放使用统一数据契约。命名空间用于隔离本次运行的数据，不能承诺数据库业务唯一性。具体配置见[运行时数据](agent-data.md)。

HTTP 配置支持请求头、身份/鉴权、会话与有限重试策略。凭据通过环境变量引用，报告统一脱敏。重定向、来源和实际目的地址受访问策略约束。

## 断言与依赖

执行验证状态码、响应契约、明确值及业务规则。异步轮询只重查条件，不自动重放写请求。多步骤链路通过明确的导出和类型化引用连接；前置准备和清理也需授权范围与预算。

接口 Mock 使用项目已配置路由，为受控依赖提供响应，不把 Mock 结果冒充真实上游验收。项目方外部夹具通过显式配置和自检接入；不允许模型加载任意脚本。

## 报告与知识

报告保存执行范围、请求摘要、断言、失败和清理状态。AI 解释和原始判定分开。确认后的规则可进入项目上下文，冲突、过期和未审核知识按治理规则处理。

相关配置在控制台「配置中心」；隔离与允许列表见[执行隔离](sandbox.md)，Agent、配方、MCP 和用量见[运行时数据与 Agent](agent-data.md)。
