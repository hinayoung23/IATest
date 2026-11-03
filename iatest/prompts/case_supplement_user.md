请基于“补充场景说明”为现有接口测试用例集追加测试用例。

生成要求：
- 只生成补充用例，不要重写、解释或输出已有用例。
- 新用例必须覆盖用户补充场景说明中明确提出的场景。
- 不要生成与“已有用例”重复的场景、标题、请求数据或断言。
- 必须参考“用例生成策略”，保持 priority、tags、design_method、risk、assertions 等字段格式一致。
- 如果补充场景依赖已有数据，应优先输出 setup_steps 创建前置数据，并用 extract 提取变量给主请求使用。
- 如果补充用例会创建临时测试数据，且接口定义中存在可删除该资源的接口，应输出 cleanup_steps。
- expected_result 必须基于接口定义中的 responses、description、parameters.schema.description 或用户补充说明；接口定义没有声明的状态码不要自行补充。
- 最多输出 {{MAX_CASES}} 条用例。
- 只输出 JSON 数组，不要输出 Markdown，不要解释。

{{CONTEXT_BLOCK}}

补充场景说明：

{{SCENARIO_TEXT}}

用例生成策略：

{{STRATEGIES_JSON}}

目标接口定义：

{{ENDPOINTS_JSON}}

已有用例：

{{EXISTING_CASES_JSON}}
