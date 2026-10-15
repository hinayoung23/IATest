# 运行时数据与 Agent 能力深化

以下能力纳入 1.1.0，升级方式和发布说明见 [1.1.0](releases/1.1.0.md)。相较 1.0.4 不增加生产依赖，使用已有 jsonschema、httpx 和 PyYAML。文末保留发布前的各阶段验收记录。

## 运行时测试数据

在用例 Markdown 的 `iatest-cases-json` 对象中声明生成规则，使用 `{{变量名}}` 引用：

```json
{
  "id": "TC-USER-0001",
  "title": "创建用户 - 合法数据",
  "method": "POST",
  "path": "/users",
  "data_generation": {
    "username": {"kind": "string", "minLength": 12, "maxLength": 16},
    "age": {"kind": "integer", "minimum": 18, "maximum": 65},
    "enabled": {"kind": "boolean"}
  },
  "samples": 5,
  "body": {"username": "{{username}}", "age": "{{age}}", "enabled": "{{enabled}}"},
  "assertions": [
    {"type": "status_code", "expected": 200},
    {"type": "json_path", "target": "$.username", "expected": "{{username}}"}
  ]
}
```

- `kind` 支持 string、integer、number、boolean、null、object、array，以及 email、uuid、date、date-time。格式也可在显式 `schema` 中声明。
- `schema` 支持 JSON Schema；可组合枚举、长度/范围、multipleOf、常用正则、对象、数组及本地 `$ref`。最终数据必须通过完整 Schema 校验。生成器只实现有界采样子集，复杂或不可满足的规则会失败，应改用明确数据集或 Fixture。
- 正常场景的生成变量自动与已保存的可信请求字段契约取交集，发送前再次验证完整请求。声明为负向或期望 4xx/5xx 的步骤保留原有触发输入。
- 每份样本在 setup 前生成一次；setup、链路、断言、轮询、重试和 cleanup 共用这些值。完整模板替换保留数字、布尔、对象等类型。
- `variables` 和数据集行中显式给定的同名值优先，便于保存边界值或故障触发数据。没有 `data_generation` 的固定用例保持原有行为。
- `samples` 默认 1。先展开数据集，再展开样本；例如 2 行数据、每行 3 个样本得到 6 条执行记录，保留源用例血缘并支持分片。总展开量仍受 `sandbox.max_cases` 限制。
- 不指定 seed 时每次运行使用新 seed；大值域字段通常不同，布尔、枚举和小值域可能重复。
- `unique: true` 仅用于足够大的字符串值域，通过每次运行的 `iatest_namespace` 隔离随机流，拒绝明显固定的规则。严格的业务唯一性仍由数据库或数据服务保证。可在 Fixture 参数中使用 `{{iatest_namespace}}` 创建、清理本轮资源。

示例命令：

```bash
# 每条用例执行 10 个样本
iatest run --project demo --samples 10 --no-ai --json-output results.json

# 复现逻辑数据；分区选择也不依赖执行历史
iatest run --project demo --samples 10 --seed regression-42 --no-ai

# 重放指定运行中失败样本的实际生成数据
iatest run --project demo --replay-run 12 --no-ai
```

结果增加 `generated_data`、`generation_meta` 和 `execution_meta`；元数据保存 seed、namespace、样本序号、规则/输入摘要和分区选择。原始生成值经脱敏后存入结果数据库。重放验证可信契约和规则未变化、输入摘要仍匹配；敏感值被掩码、记录被截断或历史记录被清理时拒绝重放。重放会重新执行 Fixture 生命周期，不能恢复被测系统过去的业务状态。`--replay-run` 与 `--rerun-failed` 互斥；后者使用新生成值。曾使用 `--samples` 覆盖数量时，重放应给出相同样本数。

固定 seed 不固定 `unique` 模式的 namespace；需要完全重现失败输入时使用重放。动态采样结果独立于源用例规则，参与执行证据，不进入用例生成缓存。样本覆盖信用聚合实际执行记录，任一样本失败即使源用例失败。

## 分区采样与请求预算

声明允许采样的分区，每个分区 Schema 与原规则取交集：

```json
{
  "age": {
    "kind": "integer", "minimum": 18, "maximum": 65,
    "partitions": [
      {"name": "young", "schema": {"maximum": 25}},
      {"name": "old", "schema": {"minimum": 50}}
    ]
  }
}
```

默认按最少执行次数轮转，未执行分区优先，历史失败给予有限权重。分区统计按规则指纹隔离，规则变化后重新累计。固定 seed 时按样本序号轮转，保持跨分片和历史记录的一致性；不自动推断业务分区，不改写断言或预期。

`iatest.json`：

```json
{
  "execution": {
    "max_requests": 100,
    "sampling": {"prioritize_failures": true},
    "cleanup_timeout_seconds": 10
  }
}
```

`max_requests` 默认不限，设置后在 HTTP dispatch 前原子扣数，包含环境/Fixture 准备、鉴权、重试和轮询。耗尽后取消后续主流程请求，policy/process 模式行为一致。清理使用独立时限，清理请求不占主流程额度，防止预算耗尽导致资源遗留。`execution_meta.requests_started` 是截至该结果的累计主流程请求数，不是单用例独占请求数；run 元数据 `request_budget` 保存限额与累计值。低层 API 若自行传入 HTTP 客户端，须沿用 `execution.transport.client_options()` 的请求控制钩子。

## Agent 评测

```bash
iatest agent-eval examples/evals/agent-baseline.json --trials 2 --output agent-eval.json
# 显式启用真实模型评测，需要配置并启用 llm 模型
iatest agent-eval examples/evals/agent-baseline.json --trials 3 --live --output live-eval.json
# 使用已配置的 analyzer 模型验证质量审查角色
iatest agent-eval examples/evals/agent-quality-negative.json --trials 2 --live --model-role analyzer --output quality-live.json
```

随源码提供 30 个接口识别、规划和用例设计任务，以及独立的错误候选质量审计任务。规划集包含 5 个无业务增量的空计划控制和 5 个已声明条件业务规则，避免要求规划角色重复通用覆盖。每个任务分别保存 `prompt`、可信 `endpoints`/`state`、允许工具 `tools`、离线响应 `responses` 和判定条件 `expected`。判定支持 `artifact_schema`、`required_tools`、`forbidden_tools`、`max_tool_calls`、`no_tool_errors`、`gate: "compile_cases"` 和 `compiled_schema`。文档依据可用自然语言描述，实际方法、路径、状态码与请求仍通过编译及参考 Schema 验证。

默认模式回放模型响应，但实际调用框架工具及编译门禁，适合检验协议和编排回归。`--live` 忽略回放响应，真实调用已配置模型，参考答案不进入 Prompt。每次试验使用独立临时目录及 SQLite 状态；沿用 Agent 预算与路由配置，不执行原项目 Fixture 或读取项目配方。JSON 输出包括轨迹、耗时、Token/工具消耗、单次成功率、全部试验成功率和至少一次成功率；任一失败返回退出码 1。离线成功率不代表真实模型质量，离线 Token 数是估计值。

`--model-role` 可选 master/case_generator/analyzer，默认沿用身份的模型角色及配置中的 master 回退策略；路由规则仍生效，实际 provider/model 记入每条结果。工具失败和任务失败均保存调用轨迹及错误，便于区分模型输出、协议和门禁问题。严格评测中，即使模型纠正错误后产物有效，发生过工具错误仍判定失败，不通过重试掩盖首次失败。

## Agent 持久化恢复

```bash
iatest agent-task task.txt --role case_designer --project demo --session design-01 --output candidate.json
# 进程退出后，使用相同任务文件、项目与配置恢复
iatest agent-task task.txt --role case_designer --project demo --session design-01 --resume --output candidate.json
```

`agent-task` 输出候选产物，不直接覆盖正式用例。接口识别任务可加 `--document api.md`。Python 的 `run_agent_role()` 和 `AgentRuntime.run()` 同样提供 `session_id`、`resume`，普通流程不指定会话时维持原有行为。

会话与调用账本存入项目 Agent SQLite，位于执行 worker 外。保存消息、任务内存、工具上下文、证据关联、预算和待处理模型响应；已完成调用复用结果。任务、框架源码、角色、工具契约/版本、模型、输入或运行配置变化后拒绝恢复；存活进程持有的会话不能被抢占。

非幂等写调用执行途中退出，不能判断业务副作用是否已发生时拒绝自动恢复。幂等工具应由处理器保证真实幂等性，框架声明不构成数据库事务或远端 exactly-once 保证。涉及凭据或超出状态限额的会话明确标记不可恢复，脱敏内容不会替代原状态。恢复不重置已用预算；模型请求途中退出按预留上限保守计费。已有审计证据受日志保留策略影响，应在所需证据仍保留期间恢复。

## 工具契约和任务策略

`ToolSpec` 支持完整 `input_schema`、可选 `output_schema`、`timeout_seconds` 和 `max_calls`。参数和输出统一使用现有 JSON Schema 校验，不下载远程 `$ref`。失败区分权限、输入、输出与超时。工具时限依赖处理器协作检查，返回后也检查超时；不能强制中断任意阻塞 Python 插件。需要强制资源边界时仍使用执行沙箱的进程/容器模式。

多角色顶层任务可共享预算：

```json
{
  "agents": {
    "task_budget": {
      "enabled": true,
      "max_tokens": 256000,
      "max_tool_calls": 500,
      "timeout_seconds": 1800
    },
    "model_routing": {
      "rules": [
        {"role": "case_designer", "max_endpoints": 3, "max_prompt_chars": 20000,
         "model_role": "master", "reason": "small_contract_batch"}
      ]
    }
  }
}
```

预算默认关闭。启用后跨角色、定向修复与并行审计累计；模型调用前预留 Token，完成后按 usage 结算，未知费用按预留量处理。模型请求的空闲超时取角色时限与任务剩余时间的较小值，流式返回（包含心跳）及网络重试之间检查任务总时限。该控制为协作式检查，不是任意同步网络或插件调用的硬中断。Python 自定义多角色流程可用 `task_budget(root)` 包裹共享任务范围。

路由按规则顺序选择首个匹配项，支持 `role`、`min_endpoints`、`max_endpoints`、`max_prompt_chars`；只选择已配置且启用的 master/case_generator/analyzer 模型。审计日志记录选择依据，quality_reviewer 沿用既有独立审计模型。显式规则不会依据未经测量的质量或价格自动选择模型。

## 项目测试配方

`agents.recipes.paths` 配置项目允许读取的目录，例如 `examples/recipes`。每个配方使用 `SKILL.md`：

```markdown
---
name: runtime-data
description: 创建和清理独立测试数据
version: "1"
allowed_roles: [test_planner, case_designer, case_maintainer]
projects: [demo]
---
依据接口约束声明 data_generation，使用 Fixture 准备业务实体。
保留负向触发字段，创建资源时使用 iatest_namespace。
```

`projects` 可省略表示所有项目。只扫描所配置目录及其一级子目录；最多 100 个配方，每个 64 KB。角色 Prompt 注入名称、摘要和哈希，正文由 `list_test_recipes` / `read_test_recipe` 按需读取，可通过 `sha256` 拒绝版本漂移。配方内容和版本参与用例生成缓存指纹。

配方是项目测试知识，不执行其中的脚本，不扩展工具权限，也不覆盖可信契约。路径受 sandbox 文件策略约束，受限路径须显式允许读取。

## MCP 接入

### 只读 stdio 服务

```bash
# 在框架项目根目录启动；由 MCP 客户端接管 stdin/stdout
iatest mcp
```

提供 `list_projects`、`get_contracts`、`get_cases`、`get_coverage`、`get_results`；分页查询使用 `project`、`offset`、`limit`，limit 最大 100。只读取已有产物，不运行接口测试或调用模型；项目名、路径、符号链接、输入/输出 Schema 和消息大小均校验。stdout 仅输出 UTF-8 JSON-RPC，消息上限 1 MB；当前提供 tools 能力，不提供 shell、任意路径读取或写操作。

MCP 客户端配置中 command 使用已安装的 `iatest`，args 使用 `["mcp"]`，cwd 使用项目根目录。若客户端没有 cwd 配置，可使用绝对路径的可执行文件配合客户端支持的工作目录设置；服务不接收任意路径参数。

### HTTP 测试数据服务

Native Fixture 支持 `type: "mcp"`，通过明确配置的 Streamable HTTP 工具准备和清理数据：

```json
{
  "fixtures": {
    "providers": {
      "test-data": {
        "enabled": true,
        "type": "mcp",
        "url": "http://127.0.0.1:9000/mcp",
        "prepare": {
          "tool": "create_test_user",
          "arguments": {"namespace": "{{iatest_namespace}}"},
          "extract": {"user_id": "$.id"}
        },
        "cleanup": {"tool": "delete_test_user", "arguments": {"id": "{{user_id}}"}}
      }
    }
  },
  "sandbox": {"allowed_origins": ["http://127.0.0.1:9000"]}
}
```

用例声明 `fixture_requirements: ["test-data"]` 后通过 `{{user_id}}` 使用实体。可在 provider `headers` 中使用既有环境变量引用配置鉴权。桥接支持 initialize/initialized、会话标识、协议版本头、JSON/SSE 响应、结构化结果与会话释放；校验网络策略并沿用请求预算、超时、响应配额和清理机制。

HTTP 适配只调用显式配置的工具，不执行任意 stdio 命令，不支持服务端主动要求客户端执行工具、OAuth 自动发现或自动重放有副作用的失败请求。外部 MCP 数据服务由项目方提供。静态 Fixture 检查不会调用准备工具，实际准备/清理应使用现有 exercise 检查模式。

协议依据：[MCP transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)、[MCP lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)。

## 模型无关的 Token 优化

1.1.0 默认启用，无新增生产依赖。供应商前缀缓存、特定模型思考参数及价格路由未纳入；自然命中的缓存只记录服务实际返回的用量。

```json
{
  "agents": {
    "token_optimization": {
      "enabled": true,
      "evidence_dedup": true,
      "prefetch": true,
      "incremental_audit": true,
      "adaptive_batches": true,
      "prefetch_max_tokens": 18000,
      "batch_input_tokens": 18000,
      "audit_output_tokens": 6000
    }
  }
}
```

`enabled: false` 关闭本轮优化，便于对照。子开关可分别关闭。Token 阈值按字符估算，范围 1024–64000；实际输入仍受角色、模型上下文和可选共享任务预算约束。

| 能力 | 行为与边界 |
|---|---|
| 用量统计 | 保存每轮 raw usage、输入/输出、缓存读写、推理子集、估算标记、HTTP 尝试、失败未知费用、耗时及输入构成估算；旧数据标记 legacy，不补造计费字段 |
| 无损证据去重 | 审计包重复的长 Schema、对象、数组或文本进入同包事实字典。`$iatest_fact` 按内容哈希引用完整原文；本地验证展开完全等价，未缩小或保留键冲突则使用原包；本地审计校准始终使用完整原证据 |
| 固定读取预取 | 审计角色读取信息与完整包，设计角色读取最多 8 个批内接口。执行经过同一工具权限、Schema、超时和预算控制，逐次保存证据；包过大或没有相应授权时保留原工具流程，预取不执行写入或被测接口 |
| 增量复审 | 仅适用于至少 10 条用例、可信契约未变、同模型/角色/工具/代码/审计规则与策略已批准的高置信无问题基线。按 ID 比较完整内容，扩展共享接口、步骤、规则、夹具、资源、覆盖分区、数据集与身份的传递闭包；保留当前完整证据及全局内容/执行哈希索引。影响超过 40%、改动超过 25%、存在执行重复或未知结构时回退全量；增量连续最多 3 次，然后重新全审 |
| 工具与预算 | 未配置配方时移除相应工具；复杂场景在原接口/场景数量上限内按序列化契约量再分批，单个超大契约仍交给原上下文门禁；设计输出按场景数估算，审计初始输出上限 6000，重试扩大至用户配置上限 |

增量裁决仍由全部唯一模型独立执行，四维分数取基线和新裁决的较低值；严格平均分、可靠性/完整性下限、分差和 critical 否决均保留。首次审计、低置信、有问题或非 approved 的基线、契约变更、基线记录被清理均不能复用。脱敏改变原输入时无法证明原内容未变，采用完整审计；原始内容只在内存计算哈希，不写入审计正文，不同敏感值也不能命中同一完整审计缓存。生成诊断及当前覆盖清单可以变化，但仍完整提供给审计；其它证据必须一致。缓存键包含角色、工具契约、代码、模型参数、审计规则、门禁诊断与策略，完整生成缓存也包含优化配置。

预取记录进入持久化会话，恢复沿用事实与原预算；JSON 读取范围的元组/数组等价不会被误判为脱敏。敏感或超大快照继续阻止自动恢复。

```bash
# 只读查询最近 500 个 Agent 运行，输出按角色、实际供应商和模型分组
iatest token-stats --project demo --limit 500 --output token-usage.json

# 在源码目录显式调用所有已配置真实审计模型；使用独立临时项目和合成契约
.venv/bin/python scripts/validate_token_optimization.py --output .iatest/token-acceptance/live.json
```

缓存和推理字段是 Token 用量子集，不能再次加到 total_tokens；未返回的明细记为 0，不表示供应商实际没有使用该功能。失败请求标记费用未知，Token 数与估算分项不能换算成账单。A/B 验收同时检查全量正例、局部修改正例和故意错误的局部修改反例，比较实际用量、调用次数和耗时；合成样本的节省比例不代表所有业务项目。

本轮还修复了本地断言门禁的误判：简单对象 Schema 已通过 required 保证字段存在，并用 const 或单值 enum 保证明确的预期值时，视为已有有效断言，不再要求模型补充相同 JSONPath。可选字段、仅有类型或多值枚举、不同固定值仍不能替代精确值断言；引用及其它方言保留原门禁。

### Token 优化验收（2026-10-15）

最终全量 `.venv/bin/pytest -q` **468 项通过，16.94 秒**，其中 Token 优化新增 46 项。覆盖可逆去重、引用冲突、完整/增量缓存失效、依赖闭包、脱敏与非 approved 基线拒绝、预取权限/预算/恢复、失败费用未知、旧库只读统计和 Schema 断言等价。离线 30 任务 × 2 次仍为 **60/60 通过**。

三种既有模型（kimi-k2.6、glm-5.2、mimo-v2.5-pro）对 20 个独立服务契约进行一次真实配对试验，所有唯一审计模型及原准入门禁保留：

| 样本 | 审计结果 | 记录 Token | 模型调用 | 耗时 |
|---|---|---:|---:|---:|
| 相同完整候选，优化关闭 | 通过 | 76,613 | 9 | 95.45 秒 |
| 相同完整候选，优化开启 | 通过 | 55,756 | 3 | 127.14 秒 |
| 局部修改，优化开启 | 通过 | 46,647 | 3 | 201.85 秒 |
| 局部状态断言故意改为 500 | 拒绝，三个模型均指出 critical | 39,589 | 3 | 121.23 秒 |

相同完整候选的 Token 下降 **27.22%**，模型调用下降 **66.67%**；本次耗时增加，不宣称提速。局部修改中 MiMo/GLM 各复用 19 条未变用例，Kimi 因先前存在问题记录采用完整审计；后续错误修改时三个模型均进行了增量复审并检出错误。局部修改与原始完整候选不同，其消耗差值不视为同候选配对结论。GLM 设计预取另验收 **2/2 通过**，3,800 Token、2 次模型工具调用，产物通过编译门禁。

初始试验用重复描述扩大 Schema，被审计判定为数据质量问题，未获得通过基线；失败结果保留于开发机的 `.iatest/token-acceptance/initial-fixture-rejected.json`。修正为有真实字段约束的共享结构后，正例通过本地确定性门禁；仅错误候选刻意不传调用方的状态门禁，以验证模型错误检出。最终报告为 `live-v2.json`、`designer-live.json` 和 `acceptance-summary.json`，属于开发机验收记录，不随安装包分发。这是合成样本的一次真实试验，不代表长期稳定性或实际业务费用变化。当时的 wheel/sdist 开发构建位于 `/private/tmp/iatest-token-optimization-build-20261015/`；正式版本使用 1.1.0 的独立构建。

## 本地验证

回归涵盖动态数据类型及约束、固定 seed、数据集/分片、真实 HTTP、进程 worker、失败重放及脱敏拒绝、跨字段契约、分区调度、请求预算与清理、真实进程退出恢复、非幂等副作用阻断、完整工具契约、共享预算、路由、配方缓存，以及 MCP stdio 和 HTTP JSON/SSE。

2026-10-14 在 macOS / Python 3.14.3 上，补充真实验收修复后全量 `.venv/bin/pytest -q` **422 项通过**；离线基线 30 个任务 × 2 次，共 **60/60 通过**，负向审计控制 **2/2 通过**。wheel/sdist 构建、twine 元数据检查、包内容检查，以及 wheel 解包布局中的生成器、角色权限、评测与 MCP 初始化均通过。

真实模型验收先后完整运行 24/30、29/30 通过，发现评测职责冲突、依据字符串误判、断言类型/Schema 声明不足，以及短证据读取的工具参数问题。修正评测语义和工具契约后，重测受影响的全部设计任务。最新分批结果为 Kimi 接口识别 10/10、GLM 业务规划 10/10、GLM 设计 10/10、MiMo 错误候选审计 2/2；合计 32 个样本通过，包含不同批次的复验，不是一次性完整执行。基础任务各 1 次真实试验，审计重复 2 次，原始失败结果保留在 `.iatest/acceptance-20261014/`，汇总为 `acceptance-summary.json`。

真实评测使用合成任务和真实模型 API；恢复、MCP、沙箱及业务 HTTP 执行的验证来自本地集成测试。本轮未在 Windows、Linux 或 Docker 运行新增集成测试；这些环境和长期重复稳定性需要后续验收。详细记录同步至本地实现方案第二十节。本地构建保存在 `/private/tmp/iatest-agent-evolution-build-20261014/`，属于未发布的开发验证产物。
