# 测试用例字段格式规范

数据集、variables、identity、poll、resources、parallel_safe、retry_safe、json_schema 和 response_time 的完整配置与示例见[框架补充与使用说明](framework-improvements.md)。这些字段在 Markdown 的 `iatest-cases-json` 对象中保存；数据集行 ID 只在执行时展开。

## 用例编号 id

统一格式：`TC-模块-四位起步序号`

示例：

- `TC-EMAIL-0001`
- `TC-USER_AUTH-0002`
- `TC-EMAIL-1000`

规则：

- 固定以 `TC-` 开头。
- 模块名只使用大写英文字母、数字和下划线。
- 同一模块内序号从 `0001` 递增。
- 超过 `9999` 后自然扩展为 `10000`、`10001`，不截断、不回绕。
- AI 输出、历史用例读取和保存前都会自动归一化。

## 用例名称 title

建议格式：`接口名称 - 场景名称`

示例：

- `生成邮箱 - 正常场景`
- `检查邮箱 - Path 参数 email 非法格式`

规则：

- 使用简洁中文。
- 不应只写接口路径。
- 最长保留 120 个字符。

## 用例等级 priority

只允许使用：`P0`、`P1`、`P2`、`P3`

| 等级 | 含义 |
|------|------|
| P0 | 最高等级，阻塞级、核心冒烟、主链路、必须优先处理 |
| P1 | 高优先级重要回归 |
| P2 | 常规负向、边界值、兼容场景 |
| P3 | 低风险探索性或补充场景 |

兼容映射：

- `critical`、`blocker`、`致命` → `P0`
- `high`、`major`、`高` → `P1`
- `medium`、`normal`、`中` → `P2`
- `low`、`minor`、`低` → `P3`

## 设计方法 design_method

只允许使用 snake_case：

- `happy_path`
- `optional_field`
- `required_field`
- `type_error`
- `boundary_value`
- `path_parameter_error`
- `dependency_chain`
- `auth_error`
- `equivalence_class`
- `status_code_coverage`

## 标签 tags

规则：

- 使用 snake_case 字符串数组。
- 必须包含对应的 `design_method`。
- 不使用中文标签、空格或驼峰写法。

## 风险 risk

只允许使用：

- `none`
- `negative_behavior`
- `behavior_dependent`
- `data_dependent`
- `data_dependent_without_fixture`
- `destructive`
- `env_dependent`
