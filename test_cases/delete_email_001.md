# [delete_email_001] 删除邮箱成功

## 基本信息
| 属性 | 值 |
|------|-----|
| 用例ID | delete_email_001 |
| 接口路径 | DELETE /api/delete/{email} |
| 用例等级 | P0 |
| 设计方法 | happy_path |
| 标签 | delete |

## 请求信息
- 方法：`DELETE`
- 路径：`/api/delete/{email}`
- 路径参数：`{'email': 'test123@mail.tempbox.cn'}`
- Query：`{}`

## 断言列表
| 序号 | 断言类型 | 断言目标 | 操作符 | 期望值 |
|------|----------|----------|--------|--------|
| 1 | status_code | - | eq | 200 |

## AI 生成说明
- 当前版本使用规则生成 happy_path 用例；AI 生成会在后续版本接入。
- 生成依据：接口定义
