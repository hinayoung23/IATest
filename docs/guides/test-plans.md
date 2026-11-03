# 类型化计划与跨类型工作流

## 开始使用

```bash
# Web UI 使用已安装的 Chromium；不下载浏览器，也不需要 Playwright 运行时。
export IATEST_CHROMIUM_PATH="/path/to/chromium"
iatest test capabilities
python examples/automation_lab.py --port 8766
```

另一个终端：

```bash
iatest test import-plan examples/plans/web.json --project demo
iatest test run web-login --project demo
iatest test reports --project demo
iatest web --project demo
```

控制台的「测试工作台」按界面与交互、服务与数据、专项质量、自动化流程组织场景；设备/能力检查、资产和结果分别归入设备与环境、测试资产、执行与报告。界面测试默认采用 AI 目标探索：输入业务目标和具体预期，观察、操作及独立回放后保存目标用例与计划。手工编排可创建基础计划，高级 JSON 可表达复杂步骤、契约变异和类型化数据引用。Web 手工编排页可单独查询目标，查询结果标为「仅观测，未判定」。普通 AI 草稿需要另行运行；AI 目标探索由受授权、预算与来源约束的工具执行动作。

所有例子仅针对本地测试服务。`/vulnerable-order` 是专门用于规则对照的已知越权样本，演示服务只监听 `127.0.0.1`。

## 统一契约与编排

外层字段是 `id`、`kind`、`contract_version:1`、`title`、`spec`、`budget`。通用预算包含 `timeout_seconds`、`max_requests`、`max_actions`、`max_queries` 和 `max_evidence_bytes`，各引擎消费适用的预算。HTTP 子计划和整条运行分别限制请求次数；重定向也计数。Web 使用动作、查询、协议和截图限额，浏览器 HTTPS 隧道中的实际 HTTP 请求数目前不可精确计量，不能把 `max_requests` 当成浏览器流量硬上限。

结果中 `lifecycle` 与 `verdict` 分开：正常完成但门禁失败是 `completed/failed`；缺少能力、证据不全、预算耗尽或操作派发未知不能通过。CLI 非通过结果退出码为 1；Web 任务完成不等于测试通过，要看类型化结果的判定。

兼容 API Markdown 用例时，`case_ids` 选择用例，`run_scope` 只保留现有报告范围标签，不会根据标签自动筛选用例。原 CLI/Web 的筛选和运行选项继续使用原入口。

`workflow.spec.nodes` 支持 `depends_on`、`always`、`resource_owner`、显式 `resources` 和嵌入的 `plan`。默认串行，可设置 `spec.max_parallel`（1–32）执行独立分支；相同资源、本地设备别名或相同远程节点自动互斥。`resource_wait_seconds` 限制可取消的资源等待。前置失败会阻断依赖，独立步骤仍可运行。控制台基础表单可选择独立分支与并发数，高级 JSON 表达任意受支持 DAG。

`always:true` 表示所有普通分支收敛后的最终清理，取消后仍有独立总计 15 秒及共享请求预算（默认 100，可用 `cleanup_max_requests` 限定）。清理也必须显式授权目标和动作；输入缺失或资源所有权不明不会猜测操作。正常节点不能依赖最终清理。并发本地/远程任务原子共享父请求额度，远程先预留、已知完成后返还未用额度；未知任务保守记账，不自动重放。

API 请求计划可从最后一个完整 JSON 响应导出：

```json
"exports": {
  "order_id": {"path": "$.id", "type": "string", "sensitive": false}
}
```

后续节点在类型化字段中引用：

```json
{"$from":"prepare.order_id","type":"string","sensitive":false}
```

引用必须指向声明过的前置节点，类型和敏感级别必须与导出定义一致。敏感数据仅在运行内传递，结果中遮蔽，并遮蔽后续证据中相同的字符串；不会把脱敏后的存储内容当作可恢复凭据。跨引擎中断写入不自动恢复或重放。

## 选择引擎

浏览器和桌面见[浏览器测试](browser-testing.md)、[桌面测试](desktop-testing.md)，原生设备见[移动端](mobile-testing.md)。数据库及会话协议见[数据与协议](data-protocol.md)，性能/安全/无障碍见[性能与安全](performance-security.md)。

计划的能力和外部条件可通过 `iatest test capabilities`、`iatest test doctor` 和设备预检检查。高级 JSON 与 CLI 可表达所有对应引擎已声明的字段。
