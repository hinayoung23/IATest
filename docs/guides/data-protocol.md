# 数据质量与协议

## 数据质量

`kind: data` 支持显式 records 或只读数据库来源。检查包括 Schema、unique、not_null、references 和 count。结果保存失败行号与数量，不复制完整记录值；超出结果额度时拒绝整次读取，不用截断结果制造通过。

SQLite 使用只读 URI、query_only、函数/表授权与禁用扩展：

```json
{"id":"account-quality","kind":"data","spec":{"database":{"driver":"sqlite","path_env":"TEST_DATABASE_PATH","query":"select id,email from accounts where id > ?","parameters":[0],"allowed_tables":["accounts"],"max_rows":1000,"timeout_seconds":5},"checks":[{"id":"unique-account","rule":"unique","path":"$.id"}]}}
```

路径必须通过项目读取策略。进程/容器使用包含已提交 WAL 的只读备份快照，不让 worker 自行读取原数据库。

PostgreSQL/MySQL 安装 `iatest-framework[database]`。连接使用环境变量引用、TLS、只读事务、SQL AST 与允许表检查；拒绝写入、越界表、未授权函数及不明确的 SQL。凭据不写入计划。详细字段使用控制台数据质量的 JSON 编辑器和 `iatest test capabilities` 检查。

## PostgreSQL/MySQL 数据源

安装 `pip install 'iatest-framework[database]'`；这是可选依赖组，默认安装和 SQLite 不加载这些驱动。示例见 [PostgreSQL 示例](../../examples/plans/postgresql-readonly.json) / [MySQL 示例](../../examples/plans/mysql-readonly.json)，通过既有数据质量 CLI/Web JSON 编辑器保存、执行和查看报告。

网络库使用环境变量凭据引用、显式主机/端口/库、表白名单和网络授权。与原生协议相同，以 `https://host:port` 表示 TLS 连接的允许来源；这是授权标识，不向数据库发送 HTTP。默认验证服务器证书及主机身份；仅回环地址可显式 `tls: disable`，可用 `tls_ca_env` 引用受读取策略授权的 CA 文件。解析后锁定本次目标 IP，仍检查配置及父级网络范围。

查询使用位置参数 `?`，解析后转换为各驱动绑定参数，字符串中的百分号保留。SQLGlot 检查单条 SELECT、所有 CTE/子查询的直接表引用、命名空间和有限纯函数，再规范化 SQL；拒绝写入、锁、跨库、递归、自定义函数和未支持语法。PG 使用固定 pg_catalog 函数搜索路径，物理表绑定显式 schema。两种数据库均确认只读事务和基础表，拒绝视图/外部关系，不宣称支持任意 SQL。

使用专用只读数据库账号；连接器不创建或修改业务账户。结果最多10000行、100列、4MiB；总查询窗口最多30秒，到期/取消中断，不以截断结果放行。Decimal 保持精度转字符串，日期时间为原时区语义的 ISO 字符串，二进制和非有限数值拒绝。进程执行只传明确凭据引用和授权 CA，SQLite 仍使用只读 backup 快照。

接口依据：[Psycopg 只读事务](https://www.psycopg.org/psycopg3/docs/api/connections.html#psycopg.Connection.read_only)、[PyMySQL 连接与 TLS 参数](https://pymysql.readthedocs.io/en/latest/modules/connections.html)、[SQLGlot 解析与 AST](https://sqlglot.com/)。



## WebSocket 与 MQTT

协议计划显式声明 endpoint、allowed_origins 和有限 exchanges，支持发送、接收与确定性消息断言。TLS 验证证书，未知投递停止相应会话，不自动重放发布。

```json
{"id":"mqtt-roundtrip","kind":"protocol","spec":{"transport":"mqtt","mqtt_version":"5.0","endpoint":"mqtt://127.0.0.1:1883","allowed_origins":["http://127.0.0.1:1883"],"exchanges":[{"op":"subscribe","topic":"owned/test","qos":2},{"op":"publish","topic":"owned/test","qos":2,"text":"hello"},{"op":"receive","topic":"owned/test","expect_text":"hello"}]}}
```

MQTT 支持 3.1.1/5.0、QoS 0/1/2、有限队列、确认握手、属性与 keepalive。QoS2 单次交付范围是当前 clean-start 会话，不包含跨连接持久化恢复；QoS0 需要后续消息或业务断言，不凭无确认的发送通过。

## gRPC

运行时使用 h2 与 protobuf。描述符由已有 protoc 在显式输入范围内冻结，不执行模型生成的 Python：

```sh
iatest test proto-compile ./proto echo.proto --output descriptor.json
```

`spec.protobuf` 保存描述符 base64 和 SHA256；交换声明 rpc、messages、expected_messages 和 expected_grpc_status。支持 unary、client/server streaming、bidi 及同流 dialogue 的交替收发。HTTP 200 不等于 RPC 成功，必须核验 grpc-status、消息结构和数量。

所有流都有时间、消息数、字节与接收队列上限。结构化比较遵守 protobuf JSON 的字段默认值及数值语义，不将多条消息拼接成 unary 结果。

## gRPC 会话内交互

`examples/plans/grpc-interactive.json` 提供冻结描述符及 `dialogue` 示例，CLI 和协议控制台沿用 JSON 计划入口。每个 RPC 在同一个 HTTP/2 stream 内显式 `send`、`receive`、`half_close`，可在半关闭前读取服务端消息，并将最后一次已断言回复的字段通过 `{"$reply":"$.text"}` 绑定到下一次请求。每个 receive 必须有 `expected_message` 或自包含 `expected_schema`；不保存消息正文。示例的 test.Echo/Call 是演示协议，业务使用自己的冻结描述符及受信预期。

`dialogue_timeout_seconds` 最多600秒，同时服从父任务和 load 窗口；`max_messages` 最多10000，计算发送和接收总数。单条1MiB、传输32MiB、未消费接收队列4MiB，逐帧处理流控和分片。要求恰好一次半关闭，正确 HTTP 状态、gRPC 类型、尾部 grpc-status 和流结束，拒绝额外未断言回复、压缩或残缺帧。取消、超时、RST、断连后没有自动重放。


## 协议负载

WebSocket、MQTT、gRPC 可使用有限 load 及 distribution.nodes。rate 单位为会话/秒，不是消息 RPS。全局并发、请求预算和到达率按节点分片，汇总原始样本计算分位数；不能平均节点 P95。

控制台在「数据质量」「协议契约」中创建、保存、运行和查看证据；结果可加入[跨类型工作流](test-plans.md)及[版本任务](business-testing.md)。
