# 接口自动化能力补充与修复

1.0.4 保留 Markdown 用例文件和原有命令，数据驱动、鉴权和插件等能力按需配置；文件和网络访问策略默认启用。新增生产依赖为 `jsonschema>=4.23,<5`。升级与发布说明见 [1.0.4](releases/1.0.4.md)。

## 已修复的问题

| 问题 | 当前行为 |
| --- | --- |
| 失败用例返回成功退出码 | `iatest run` 失败返回 1；取消或总超时返回 130 |
| 导入文档覆盖执行环境地址 | 优先使用 `--base-url`，其次是选中环境；仅环境地址缺失时使用导入地址 |
| JSON Schema 被降级为对象类型断言 | 保留完整 Schema，校验 required、嵌套对象、数组、范围、组合、格式等约束 |
| 缺失响应头通过 exists 断言 | 缺失值与 null 区分；响应头名称不区分大小写 |
| 报告、原始响应及模型证据泄露 token | 报告、SQLite、JSON/JUnit 和 Agent 证据共用脱敏策略，包括原始 JSON 文本和失败信息 |
| 不同用例共享 Cookie | 用例独立 Cookie 容器；同条链路的步骤、setup 和 cleanup 共享会话 |
| 业务规则变化未触发接口差异 | business_rules 纳入接口签名与缓存失效判定 |
| 默认样例违反 minimum 等约束 | 按约束生成并再次校验；无法生成时明确要求 example 或数据集 |
| 递归 OpenAPI 引用丢失根定义 | 抽取 Schema 时补齐本地 `$defs`；可选递归字段生成有限样例 |
| 业务项目路径和固定 TTL 混入通用逻辑 | Tempbox 特有链路移至 `adapters/tempbox.py` 并限制接口作用域；不再默认推断 600 秒 |
| 仅发布时跑测试、Windows 无 fcntl | 增加 push/PR 测试工作流，配置 Linux/macOS/Windows；Windows 使用 msvcrt 文件锁 |

JSON Schema 执行阶段不会下载远程 `$ref`。OpenAPI 导入负责解析、绑定引用；直接编写断言时应提供完整的本地定义。格式校验按当前安装的 jsonschema FormatChecker 能力执行。自动样例生成覆盖常见约束；复杂正则、无法满足的组合和必填递归模型需要项目提供样例，框架不会生成不合法数据后继续执行。

## CI 执行

```bash
iatest run --project demo --env staging --no-ai \
  --tags happy_path,dependency_chain --priority P0,P1 \
  --seed regression-42 --timeout 120 --retries 1 \
  --junit artifacts/results.xml --json-output artifacts/results.json

iatest run --project demo --shard 1/3 --no-ai
iatest run --project demo --rerun-failed --no-ai
```

标签匹配任一个给定标签，优先级另作过滤。分片以用例 ID 的 SHA-256 稳定划分，数据集展开后再分片。`--cases` 可以选择源用例或单个展开后的行 ID。未知用例 ID 会报错。

| 退出码 | 含义 |
| --- | --- |
| 0 | 实际执行全部通过 |
| 1 | 存在失败；或设置 `--fail-on-flaky` 且存在重试恢复的用例 |
| 5 | 空执行集或全部跳过 |
| 130 | 取消或执行总时限耗尽 |

`--allow-empty`、`--allow-all-skipped` 可以明确允许对应情况返回 0。`--no-ai` 不调用模型生成或审计运行报告。JSON/JUnit 在模型报告阶段之前导出。

只读用例可安全重试；含写请求、夹具或生命周期步骤的用例默认不自动重试。确有幂等保证时可设置 `retry_safe: true`。报告、JSON/JUnit 和 SQLite 保留重试前失败证据；恢复后标记 `flaky`。

## 数据驱动与可重复输入

在 `iatest-cases-json` 的用例对象中添加：

```json
{
  "variables": {"tenant": "demo"},
  "dataset": [
    {"_id": "small", "variables": {"limit": 10}},
    {"_id": "large", "variables": {"limit": 20}}
  ],
  "query": {"limit": "{{limit}}", "tenant": "{{tenant}}"}
}
```

展开后的 ID 为 `原ID[small]`、`原ID[large]`。行变量覆盖用例变量，各行上下文和 Cookie 独立。也支持 `dataset: {"source": "data/users.json"}`、CSV 文件、`{"matrix": {"region": ["east", "west"], "limit": [10, 20]}}`，以及 `iatest.json` 中 `datasets` 注册的名称。

Web 用例编辑的“请求数据”JSON 也可以填写 variables、dataset、identity、poll、resources、parallel_safe、retry_safe；已有配置在编辑后保留。

JSON 行可通过 `request_overrides` 覆盖 headers、query、path_params、body。CSV 值保持字符串；需要数字、布尔或嵌套对象时使用 JSON。单数据集最多 10000 行、文件最多 20 MB。变量名称沿用 `{{name}}` 语法，`_iatest_` 前缀保留给框架。

固定 `--seed` 可复现同一用例的 uuid、random 和 timestamp 输入，不能复现服务端时间或外部状态。timestamp 是用于唯一命名的数字字符串，固定种子时不代表真实时钟。内置变量在同条用例的准备、请求、清理与重试中保持稳定。

## 鉴权与 HTTP 配置

```json
{
  "environments": {
    "staging": {
      "base_url": "https://api.example.invalid",
      "verify_tls": true,
      "trust_env": false,
      "follow_redirects": false,
      "auth": {
        "default_identity": "reader",
        "identities": {
          "reader": {"type": "bearer", "token": "${READER_TOKEN}"},
          "admin": {
            "type": "oauth2",
            "token_url": "/oauth/token",
            "client_id": "${OAUTH_CLIENT_ID}",
            "client_secret": "${OAUTH_CLIENT_SECRET}",
            "scope": "admin"
          }
        }
      }
    }
  }
}
```

用例 `identity: "admin"` 选择身份。OAuth2 支持 client_credentials、可配置表单、token_path/expires_path、过期前刷新及 refresh_token；同身份并发获取有互斥缓存。只读请求收到非预期 401 时最多刷新重试一次，写请求不自动重放。显式用例 Authorization 优先；用例 `headers: {"Authorization": null}` 明确禁用鉴权，适合未登录负向测试。

HTTP 环境还支持 `proxy`、`cookies`、`verify_tls` 为 CA 文件路径，以及 `client_cert: {"certfile": "client.pem", "keyfile": "client.key"}`。这些字段仅消费项目配置；本轮修改没有写入任何项目凭据。

## 断言与异步轮询

```json
{
  "assertions": [
    {"type": "status_code", "expected": 200},
    {"type": "json_schema", "expected": {"type": "object", "required": ["ready"], "properties": {"ready": {"type": "boolean"}}}},
    {"type": "json_path", "target": "$.ready", "expected": true},
    {"type": "response_time", "operator": "lte", "expected": 500}
  ],
  "poll": {"timeout_seconds": 10, "interval_seconds": 0.5, "max_attempts": 20}
}
```

用例和 CHAIN 步骤都支持 poll。每次读取到达成功断言后停止；响应保存轮询次数和简要历史。默认只轮询 GET/HEAD/OPTIONS，写请求需明确 `allow_mutation: true`。response_time 单位毫秒，衡量本次 HTTP 请求与响应读取时间；整条用例耗时另含等待、setup、cleanup。

`execution.total_timeout_seconds` 或 `--timeout` 限制整次执行。Web 任务面板支持取消，CLI 的 Ctrl+C 也进入取消流程。policy/off 模式在请求、响应块、等待、资源锁或进度检查边界协作取消；已阻塞的 HTTP 操作仍受 HTTP 超时约束，任意阻塞插件无法在主进程中强制中断。process/container 模式由父进程监督硬时限，宽限期后可强制终止工作进程。取消后剩余用例标记 cancelled，并以 `execution.cleanup_timeout_seconds`（默认 10 秒）尝试清理；强制终止不保证远端清理完成，报告保留未确认状态。

并行链路需设置 `parallel_safe: true`；否则链路保持串行。用例 `resources: ["shared-account"]` 声明共享资源，同次运行中相同资源互斥。互斥范围为当前执行进程；多进程 CI 分片需要独立账号、数据命名空间或服务端隔离。

## 夹具自检与示例

```bash
iatest fixtures-check --project demo
iatest fixtures-check --project demo --probe
iatest fixtures-check --project demo --exercise
python3 examples/test_support_server.py --port 8799
```

默认只检查配置、启用状态、所需能力和插件注册，不调用 prepare。`--probe` 仅调用提供者配置的 `check` GET/HEAD 地址；`--exercise` 实际执行 prepare/cleanup。夹具准备失败后仍会尝试清理已准备的资源，已提取的变量保留给清理步骤；没有资源标识的清理记为 not_needed。

将 [fixture-providers.json](../examples/fixture-providers.json) 中字段合并到临时示例项目配置，可对示例服务测试时间推进与邮件注入。主请求为 `/mailboxes/{mailbox_id}`，path_params 为 `{"mailbox_id": "{{mailbox_id}}"}`：time_control 可断言 `$.expired=true`，mail_injection 可断言 `$.messages[0].subject="iatest fixture"`。每条用例分别选择所需的 fixture_requirements。

示例只改变本地示例服务状态。真实被测项目需要提供自己的测试能力接口或适配器；框架本身不能推进远端业务时钟或向实际邮箱投递邮件。

## 插件扩展

执行默认启用沙箱访问策略，插件需明确设置 `sandbox.allow_plugins=true`。独立进程、容器、网络和文件边界见 [执行隔离说明](sandbox.md)。

插件以已安装 Python 包的 entry point 注册，仅加载配置中明确启用的插件：

```toml
[project.entry-points."iatest.plugins"]
my_extensions = "my_package:register"
```

```python
def register(registry):
    registry.register_assertion("matches_id", matches_id)
    registry.register_fixture("database_seed", DatabaseFixture())
    registry.before_request.append(before_request)
    registry.after_response.append(after_response)

def matches_id(assertion, response, context):
    return None if response["json"]["id"] == context["id"] else "id mismatch"
```

`iatest.json` 设置 `plugins: ["my_extensions"]`，断言设置 `{"type": "custom", "target": "matches_id"}`。未知插件或断言明确失败。夹具提供者设置 `type: "plugin", adapter: "database_seed"`。

| 扩展接口 | 签名与约定 |
| --- | --- |
| prepare | `prepare(provider_config, context, client)` → `{"values": {...}, "cleanup_state": 任意对象}` |
| cleanup | `cleanup(cleanup_state, context, client)`；prepare 抛错时 state 为 None，也会调用 cleanup |
| before_request | `(step, headers, request_kwargs, context)`；可补充签名或请求元信息 |
| after_response | `(step, response_snapshot, context)` |
| custom assertion | `(assertion, response_snapshot, context)` → None 或非空错误字符串 |

插件需保证并行调用安全、清理幂等；可通过 `check_control()` 配合取消。插件加载不暴露为模型工具，不增加新的数据库或消息队列生产依赖。数据库、消息队列等具体连接由项目插件自行实现。

## 追溯、覆盖率与增量生成

运行记录及 Markdown/JSON/JUnit 包含环境、Git revision、框架版本与源代码摘要、契约/用例/数据/夹具/环境摘要、执行范围和 seed。摘要用于识别输入版本，不保存配置中的秘密值。结果记录包含 run_id、origin_case_id、dataset_id。

实际执行通过率为 passed/(passed+failed)，与整体通过率、skip_rate 分开。跳过与取消不计入时延百分位。覆盖清单分别统计计划覆盖、当前配置可执行覆盖、已执行与通过的规则；链路消费接口计入接口覆盖。跨环境、契约、数据、夹具或框架源代码变化的趋势标记为不可直接比较。历史记录缺少版本字段时保留原有范围/用例重合度判断。

生成缓存按接口及依赖连通组复用，接口、业务规则、关联图、相关确认经验、策略、模型或角色定义变化会使相应组失效。复用前仍跑本地门禁，组合后重新进行完整质量审计。保留全项目缓存；`iatest generate --force` 跳过两种缓存。未知的跨接口关系保守重建，缓存不会代替依赖契约。

## 验证

新增回归测试见 `tests/test_framework_execution.py`、`tests/test_framework_management.py`。覆盖 CI 退出码与导出、Schema/递归引用、脱敏、数据集/种子/分片、Cookie、轮询/取消/重试、OAuth2、资源互斥、插件、夹具真实 HTTP 准备和清理、版本趋势、独立接口缓存与依赖失效。

本地运行 `pytest -q`；Linux/macOS/Windows 验证通过 push/PR CI 矩阵执行，Python 3.11–3.14 的发布测试通过既有发布工作流执行。配置矩阵本身不代表对应运行已经通过，应以具体 Actions 运行结果为准。真实 Docker 执行需要可用的 Docker 环境。
