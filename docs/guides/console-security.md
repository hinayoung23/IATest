# 控制台、凭据与运行边界

2.1.1 的控制台默认需要本机启动授权。运行 `iatest web` 后，使用终端打印的完整 `/session?token=...` 链接；链接只使用一次，随后由 HttpOnly、SameSite=Strict 会话 cookie 访问页面、任务和证据。重启后使用新链接。不要分享授权链接、cookie 或页面中的请求 token。

控制台只绑定回环地址，校验请求 Host 和实际监听端口。每个写操作必须携带页面生成的 token；有 Origin 时还必须匹配已配置来源。无 Origin 的客户端也必须携带有效 token。取消任务遵循相同授权检查，但不等待长任务持有的项目操作锁。

## 模型连接登记

在业务工作空间执行一次本机登记，指定允许的服务与环境变量引用：

```sh
iatest llm-trust https://api.example.com/v1 IATEST_LLM_API_KEY
```

随后在设置中填写同一服务与引用。登记只保存引用，不读取或显示 Key。它独立保存在工作空间 `.iatest/llm-connections.json`，控制台不能新增信任记录。协议、主机、端口与凭据引用共同决定许可；修改服务不能将已有凭据任意发往另一站点。首次启动会迁移既有本地模型配置中的引用，后续新增服务使用登记命令。

本机回环 HTTP 模型可以直接登记；远端 HTTP 服务默认拒绝，需要显式 `--allow-http`。已有的企业代理和自定义 provider 可登记使用，不要求改成公共模型服务。应用规则不替代操作系统权限、TLS 校验和受控网络出口。

设置中区分「环境变量引用」和「输入新凭据」。新凭据写入该角色的 `IATEST_MASTER_KEY`、`IATEST_CASE_GENERATOR_KEY` 或 `IATEST_ANALYZER_KEY`，因此使用新凭据前要将相应引用与服务登记。秘密文件通过私有临时文件替换，POSIX 使用 0600，Windows 使用当前用户 ACL；不会把 Key 写入 `iatest.json`。

`.env` 不覆盖已有外部环境变量，不允许改变 PATH、TLS 信任文件或 Python/动态加载器配置。应用自己从当前工作空间导入的值可以刷新；显式终端环境变量优先。框架凭据和输入解析按工作空间读取，避免另一工作空间的自动导入值被串用。调用自定义线程或插件时，也应显式传递工作空间和所需值，不依赖进程全局环境作为项目存储。

## 反向代理与 Mock

远程入口应使用有认证的 HTTPS 反向代理，将其完整来源显式加入 `web.allowed_origins`。服务不因收到 X-Forwarded-* 头便自动信任外部来源。代理保持有效的 Host，或转发为本机 Host 并保留已登记的 Origin；仍需会话与请求 token。只有入口已承担身份验证、访问控制且部署明确需要时才设置 `web.require_session=false`。

Mock 默认只监听本机。LAN 设备测试需显式设置 `mock.allow_lan=true`，或在设置中勾选 LAN 访问。请求历史和重置接口默认只允许本机合法来源访问；需要远程管理时设置 `mock.management_token_env`，客户端通过 Bearer 令牌访问。路由响应仍可按测试需要开放给 LAN 设备。请求记录对敏感 query、header 和 JSON 字段脱敏。

## 日志、解释与证据

`IATEST_DEBUG_LLM=1` 开启私有诊断，默认只保存模型元数据与内容摘要，`0/false` 不会开启。只有显式设置 `IATEST_DEBUG_LLM_CONTENT=1` 才写入经过脱敏和截断的内容。日志位于用户 `.iatest/logs/`，限制权限并轮转；规则式脱敏无法识别所有业务隐私，内容诊断应仅用于受控测试数据。

AI 解释使用独立的 completed/skipped/degraded/failed 状态。API `build_ai_report_analysis_result()` 返回结构化结果；CLI/控制台报告元数据包含 `ai_analysis`，详细状态位于项目运行目录 `analysis-status/`。解释失败不改写确定性测试 verdict，也不再伪称已生成本地规则用例。知识版本读取失败时禁用缓存复用和保存。

图片和二进制证据仍可能包含业务隐私，不能把文本脱敏当作自动图像脱敏。优先使用明确目标区域、测试账号和合成数据；证据下载受控制台会话保护。预览和执行保留策略：

```sh
iatest test prune-results --project demo --keep 200
iatest test prune-results --project demo --keep 200 --apply
```

第一条只预览；第二条清理旧结果与对应证据，并保留被固化用例等资产引用的运行。知识库、任务状态和待恢复事务不属于可删除缓存。

## 配置、预算与排障

配置读取和保存检查类型、范围、已知键与跨字段关系，保留旧模型配置和命名环境等兼容入口。`save_config()` 合并从同一基线分出的不冲突修改，对相同字段的并发冲突要求刷新；代码更新多字段时优先使用 `update_config(root, mutator)`。

新项目默认启用跨角色任务预算；旧项目保留已配置的预算开关。角色预算仍独立生效，任务预算不等于全服务每日费用上限。Agent 审计记录共享预算快照、估算标记与重试用量。

请求、任务、引擎与模型调用共享 trace_id，分别记录阶段与 span。结构化错误会脱敏后保存；按 task_id/run_id 和 trace_id 可以关联排障。后台队列使用事件唤醒，保留低频轮询用于进程恢复。

知识全文索引可在维护时显式校验和修复：

```sh
iatest context check-index --project demo
iatest context check-index --project demo --repair
```

校验包含内容差异和 FTS 结构检查，可以识别行数相同的漂移。日常读写继续使用增量触发器，不在每次读取时全表计算摘要。
