# [check_001] 检查邮箱状态成功

## 基本信息
| 属性 | 值 |
|------|-----|
| 用例ID | check_001 |
| 接口路径 | GET /api/check/{email} |
| 用例等级 | P0 |
| 设计方法 | happy_path |
| 标签 | check |

## 请求信息
- 方法：`GET`
- 路径：`/api/check/{email}`
- 路径参数：`{'email': 'test123@mail.tempbox.cn'}`
- Query：`{}`

## 断言列表
| 序号 | 断言类型 | 断言目标 | 操作符 | 期望值 |
|------|----------|----------|--------|--------|
| 1 | status_code | - | equals | 200 |

## AI 生成说明
- 当前版本使用规则生成 happy_path 用例；AI 生成会在后续版本接入。
- 生成依据：interface_definition
