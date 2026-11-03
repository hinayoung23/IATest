请根据覆盖计划中的场景条目，生成完整的接口自动化测试用例。

## 输出格式

只输出 JSON 数组，不要 Markdown，不要解释。每条用例必须包含 scenario_id，对应覆盖计划中的条目。

生成要求与字段格式和 case_generation_system.md 一致，额外要求：

- **scenario_id 是强制字段，必须逐字复制计划条目中的值，不允许新增、改写、省略或为空。**
- 输出用例数量必须等于计划条目数量，每个 scenario_id 恰好出现一次，不可重复、不可缺失。
- CHAIN 条目的 chain_steps 已指定步骤结构，请基于该结构生成 steps 数组。
- **DELETE/POST/PUT/PATCH 接口的正向用例**：如果接口依赖关系中存在 safe_to_prepare=true 的生产者，必须生成 setup_steps 调用生产者准备数据，path_params 使用 {% raw %}{{variable}}{% endraw %} 模板引用 extract 变量，runnable=true，risk=data_dependent（不是 destructive）。不得硬编码路径参数值。

## 覆盖计划（仅生成这些场景）

{{PLAN_ITEMS_JSON}}

## 接口定义

{{ENDPOINTS_JSON}}

**强制约束：每条输出用例的 scenario_id 必须从下方覆盖计划逐字复制，不可改写、省略、新增或重复。输出用例数量必须等于计划条目数量。如果计划有 4 个条目，必须输出恰好 4 个用例，每个携带对应的 scenario_id。**

## 接口依赖关系

{{DEPENDENCY_GRAPH_JSON}}

## 已生成用例（当前批次之前的，不要重复）

{{EXISTING_CASES_JSON}}

{{CONTEXT_BLOCK}}
