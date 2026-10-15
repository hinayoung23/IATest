---
name: runtime-data
description: 为新增业务实体选择受约束的运行时变量、复现种子和清理责任。
version: "1"
allowed_roles: [test_planner, case_designer, case_maintainer]
---

成功用例的实体名称可声明 data_generation，并通过 {{name}} 引用。金额、长度、枚举与字段关系必须遵守可信接口契约；生成器不能推断业务状态码。

边界值和负向测试触发字段保持固定，或仅在同一非法分区内采样。业务实体 ID 从 setup、Fixture 或前序响应获取。不要随机化鉴权凭据或访问地址。

每条样本在准备前生成一次，同样的变量供步骤、断言、重试与 cleanup 使用。唯一资源需要命名空间及服务端唯一约束；保存 seed 与实际输入用于复现。
