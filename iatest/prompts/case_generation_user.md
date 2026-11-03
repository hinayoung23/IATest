请基于”用例生成策略”和”接口定义”生成接口自动化测试用例。

## 覆盖要求

以下要求规定了必须覆盖的场景类别。优先基于接口语义合并冗余场景，而不是机械地为每个参数组合生成独立用例。

### 1. 策略覆盖
- 每个接口至少生成 1 条 happy_path 用例。
- 启用的每一项策略（见用例生成策略列表），只要该接口适用，必须覆盖该策略对应的场景类别。如果多个策略覆盖同一语义场景，可以合并为一条用例，在 tags 中列出所有覆盖的策略名。
- status_code_coverage 与 happy_path 在只有 200 响应的接口上可以合并：一条 happy_path 用例同时标记 status_code_coverage。
- equivalence_class 对参数只有单一取值时，可以和 happy_path 合并。
- dependency_chain 策略：如果接口列表中存在明显的数据依赖链（生产资源 → 查询状态 → 更新 → 清理，仅使用当前契约和依赖图中的接口），必须输出 method=CHAIN 的链路用例。链路用例包含 steps 数组，按依赖顺序编排。
- path_parameter_error 策略：每个包含路径参数（如 {email}、{id}）的接口，覆盖空值和非法格式两类异常。如果接口文档对空路径参数有明确路由行为（如回退到列表），根据文档调整预期，不要机械生成无效用例。
- boundary_value 策略：对包含 query 参数（如 prefix、limit、offset）的接口，覆盖空值、最小值、最大值三个边界。如果某类边界在语义上和已有用例等价，可以合并。
- equivalence_class 策略：对多值枚举参数覆盖有效类和至少一个无效类。参数选项很多时选择代表性取值（首尾各一），不需要为每个枚举值生成独立用例。

### 2. 链路用例（CHAIN）
- method 为 CHAIN，path 留空。
- 必须包含 steps 数组，每个 step 包含 method、path、path_params、query、body、extract、assertions。
- 步骤之间通过 extract 传递变量。
- 每个 step 的 (method, path) 必须来自当前接口列表中的真实接口。
- 链路用例的 priority 应为 P0 或 P1。

### 3. 通用要求
- 如果某条用例依赖已存在的数据，不要硬编码不可控数据；应优先输出 setup_steps 创建前置数据，并用 extract 提取变量给主请求使用。例如：setup_steps 中 `{"method":"POST","path":"/users","extract":{"id":"$.id"}}`，主请求 path_params 中使用 `{"id":"{% raw %}{{id}}{% endraw %}"}`。
- 如果用例会创建临时测试数据，且接口定义中存在可删除该资源的接口，必须输出 cleanup_steps。
- cleanup_steps 是测试数据清理动作，即使用例失败也应尽量执行清理。
- 负向用例、破坏性用例或缺少可靠前置数据的用例可以 runnable=false，但仍应保留用例设计。
- expected_result 必须落到接口定义中的响应字段、错误响应示例或明确业务规则上；接口定义没有声明的状态码不要自行补充。
- tags 中必须包含对应策略名称，design_method 也应使用对应策略名称。
- priority 必须严格使用 P0/P1/P2/P3，不允许使用 high/medium/low。
- id 必须严格使用 TC-模块-四位起步序号，例如 TC-EMAIL-0001；同一个模块内按 0001、0002 递增，超过 9999 后继续扩展为 10000。
- design_method 和 tags 必须使用 snake_case，不要使用中文、空格或驼峰写法。
- 只输出 JSON 数组，不要输出 Markdown，不要解释。

{{CONTEXT_BLOCK}}

用例生成策略：

{{STRATEGIES_JSON}}

接口定义：

{{ENDPOINTS_JSON}}
