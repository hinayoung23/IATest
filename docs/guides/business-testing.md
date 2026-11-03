# 从用例资产到版本测试

用结构化资产组织批量用例、安装交付、客户端性能、授权节点和版本验证任务。

## 模板与批量导入

支持 UTF-8 CSV、JSON 和 XLSX。Excel 使用首个工作表，单文件最多 2 MiB、1000 条；不执行公式、宏或外部数据。目标模板和计划模板都可在控制台下载，也可以运行：

```bash
iatest test template goals.xlsx --mode goals
iatest test template plans.csv --mode plans
iatest test preview-import cases.xlsx --project demo --policy error
iatest test commit-import import-返回的ID --project demo
iatest test process-import import-返回的ID --project demo
iatest test assets --project demo
iatest test process-asset nickname-persistence --version 1 --project demo
```

通用列包括 `case_id/title/module/platform/goal/expected/preconditions/path_hints/required_steps/context/data_generation/input_env/plan/tags/priority`。`context/data_generation/input_env/required_steps/plan` 在 CSV、XLSX 单元格中填写 JSON；JSON 文件使用 `{ "contract_version": 1, "cases": [...] }`。空白步骤允许探索，缺少 `expected` 则保存为 `needs_clarification`，不会用当前页面表现替代预期。补充信息后重新导入为新版本。

预检不调用模型、不操作应用，逐行保留有效、错误和跳过状态。相同 ID 可选 `error/skip/version`，默认报告冲突；提交前复核版本，整批事务提交，不覆盖旧版本。`--valid-only` 明确允许仅提交有效行。资产保留文件摘要、来源行和版本，冻结执行时使用的计划副本。

`process-import` 只处理尚未执行的导入资产；已经开始过的失败、未确定或中断项不会自动重新操作应用。通过单条资产的显式处理可再次执行已编译计划。正在执行且完成状态未知的资产需要先检查结果，不能通过重复批处理重放写操作。

## App 获取、安装与升级

安装包规格示例：

```json
{
  "id": "android-build-2",
  "platform": "android",
  "app_id": "com.example.app",
  "url": "https://build.example.com/app.apk",
  "allowed_origins": ["https://build.example.com"],
  "headers": {"Authorization": {"$env": "BUILD_DOWNLOAD_AUTH"}},
  "sha256": "替换为64位SHA256摘要",
  "certificate_sha256": "替换为64位签名证书摘要",
  "max_bytes": 536870912
}
```

也支持 `path` 本地来源，与 `url` 二选一；摘要、期望版本和证书摘要可选。生产构建建议提供摘要及证书指纹。下载受大小、超时、重定向和来源范围约束，跨来源重定向不携带认证头；记录的来源去掉查询参数。安装包以内容 SHA-256 存储并去重，使用前重新核对内容与签名，资产 ID 不能改指其他构建。

```bash
iatest mobile fetch-app app-source.json --project demo
iatest mobile apps --project demo
iatest mobile prepare-app app-install.json --project demo
```

`app-install.json`：

```json
{
  "profile": "android-local",
  "artifact_id": "android-build-2",
  "mode": "upgrade",
  "base_artifact_id": "android-build-1",
  "seed_plan_id": "prepare-old-data"
}
```

- `fresh` 明确卸载已有应用及其数据，再安装目标构建。
- `upgrade` 使用现有安装覆盖，失败不自动卸载；应用不存在时拒绝。可先安装 `base_artifact_id`，运行显式准备计划写入旧版数据，再升级目标版本。准备计划可为 API 或同设备 UI。
- 获取包不等于安装包。安装成功后回读安装版本；启动确认只表示平台接受启动，业务就绪和数据保留必须由 UI 断言验证。

Android 使用已有 SDK 的 `aapt/apksigner/adb`，检查包名、版本、签名、最低 SDK 和 ABI；需要可用 JDK。iOS 使用已有 Xcode/macOS 工具检查 IPA 签名、证书、Provisioning 有效期与应用标识、设备授权和最低系统版本；仅支持设备 IPA，不将模拟器包混作设备包。首次启动权限、企业授权、开发者模式和签名准入仍需满足平台条件，不绕过平台限制。

执行计划可添加外层 `preparation.app`，内容同安装规格，在业务引擎执行前准备 App。直接移动 UI 计划必须使用同一设备配置；客户端性能的安装放外层。设备租约贯穿安装和运行，防止同一宿主用户的其他本地进程争抢设备；远端设备由对应节点管理。安装需要目标设备权限及合法签名；失败不自动卸载业务 App。

## 服务端业务事务性能

`performance` 用 HTTP 向被测服务施压。新增多步骤业务事务，并与单请求计划保持兼容：

```json
{
  "id": "order-load", "kind": "performance",
  "budget": {"timeout_seconds": 90, "max_requests": 20000},
  "spec": {
    "allowed_origins": ["https://test.example.com"],
    "mode": "arrival_rate", "rate_unit": "transactions",
    "rate": 20, "users": 20, "duration_seconds": 30,
    "scenarios": [{
      "id": "order", "weight": 1,
      "setup": [{"id": "login", "request": {"url": "https://test.example.com/session"}, "extract": {"session": "$.session"}}],
      "steps": [
        {"id": "create", "request": {"method": "POST", "url": "https://test.example.com/orders", "headers": {"Authorization": "Bearer {{session}}"}, "json": {"name": "{{namespace}}-{{user}}-{{iteration}}"}}, "extract": {"order_id": "$.id"}},
        {"id": "query", "request": {"url": "https://test.example.com/orders/{{order_id}}"}, "assertions": [{"path": "$.id", "operator": "eq", "expected": "{{order_id}}"}]}
      ]
    }],
    "thresholds": {"transaction_p95_ms": 1000, "max_transaction_error_ratio": 0, "max_error_ratio": 0, "max_dropped_ratio": 0}
  }
}
```

每个虚拟用户和场景独立 Cookie、初始化变量和提取上下文。`setup/teardown` 在场景中定义，事务内提取值仅供该次后续步骤使用，跨事务持久值来自 setup；需要按事务创建后删除的数据，在事务末尾添加显式清理步骤。全局 teardown 适合按测试命名空间清理。场景权重是抽样比例，不保证短运行严格达到给定配比。

区分 HTTP 错误与 HTTP 200 下的业务断言错误，独立计算请求 RPS/延迟和事务 TPS/延迟。事务错误默认不可通过，允许比例时必须声明 `max_transaction_error_ratio`。`rate_unit=transactions` 用于多步骤到达率，不能把 TPS 当 RPS。

阶段字段 `phases` 的持续时间总和必须等于 `duration_seconds`：到达率使用 `start_rate/end_rate`，并发使用 `start_users/end_users`。`pacing_seconds` 为事务起点间最短周期，`think_seconds` 为事务后等待，步骤还可单独设置等待。准备不计测量窗口，清理独立限时且计数单列；无界排队不受支持。样本超限或发压不足没有相应门禁时不通过。

## 客户端性能

`client_performance` 将一条已有 UI 计划放入 `spec.plan`，指定同平台 `platform`、`warmup`、`iterations` 和 `thresholds`：

```json
{
  "id": "web-client", "kind": "client_performance",
  "spec": {
    "platform": "web", "warmup": 1, "iterations": 3,
    "plan": {"id": "heading", "kind": "web", "spec": {"start_url": "http://localhost:8000", "allowed_origins": ["http://localhost:8000"], "steps": [{"op": "expect", "target": {"selector": "h1"}, "assert": "exists"}]}},
    "thresholds": {"max_navigation_ms": 2000, "max_action_max_ms": 1000}
  }
}
```

Web 从隔离世界的 PerformanceObserver 和导航条目观测导航、FCP、LCP 候选、长任务、事件延迟及宿主动作耗时；每次是新浏览器配置，warmup 不代表浏览器热缓存。LCP 候选不等于最终线上 LCP，事件最大延迟不等于完整 INP。Android 通过系统报告采集 `am start TotalTime`、旅程后的 PSS 快照和帧卡顿比例；支持 `startup=cold/warm`，但 warm 的进程状态需结合设备实际情况解释。PSS 快照不是峰值内存，TotalTime 不是业务完全就绪时间。

iOS 支持显式 attach/launch/device_processes 录制、CPU/内存解析、XCTest 启动和受控滚动、范围绑定校准及离线门禁。标准 Apple 附加、收尾与零帧问题按指标质量保留，不自动切换采集器或补造帧数。Web CPU 是专属 Chromium 实例存活进程的区间平均；Android 使用唯一 App 主进程的 /proc 计数及启动身份。100% 对应一个逻辑核，可能超过 100%，不代表 CPU 峰值。缺失指标为 not_assessed，不能用宽松阈值通过。详见[iOS 性能](ios-performance.md)。

## 局域网执行节点与分布式压测

不自动扫描局域网或安装节点。各机器安装相同框架源码/发行包，显式登记节点与来源、可执行类型。控制端和节点分别通过已有环境变量读取配对凭据；框架不生成或修改密钥。令牌至少 16 字符，建议由现有凭据管理系统提供。

节点登记 JSON：

```json
{"id":"worker-a","token_env":"IATEST_NODE_A_TOKEN","allowed_origins":["https://test.example.com"],"allowed_kinds":["api","web","performance","security","workflow","client_performance","android","ios"]}
```

```bash
# 控制端
iatest node enroll node-a.json --project demo
iatest node controller --project demo --host 0.0.0.0 --port 8768 --cert /path/server.pem --key /path/server.key

# 节点机器；使用可信 CA，节点本地再限制类型与来源
iatest node worker https://controller.example:8768 worker-a --project demo --origin https://test.example.com --token-env IATEST_NODE_A_TOKEN --kinds api,web,performance,security,workflow,client_performance,android,ios --ca /path/ca.pem

iatest node list --project demo
iatest node run worker-a web-smoke --project demo
```

Windows 可从控制台“执行节点”生成启动包，也可使用以下 CLI；需要匹配控制端源码的当前 wheel、公共 CA 证书和可达控制端地址：

```bash
iatest node windows-bundle windows-node.zip dist/iatest_framework-1.1.0-py3-none-any.whl /path/controller-ca.pem https://controller.example:8768 windows --origin https://test.example.com --token-env IATEST_NODE_A_TOKEN --project demo
```

在 Windows 解压后运行 `python .\start-node.py`，可通过 `--python-path` 指定已有 Python 3.11+，或通过 `--workspace` 指定尚不存在的独立目录。Python 入口只使用标准库，适用于系统禁用 PowerShell 脚本的环境；不修改执行策略。也保留 `powershell -NoProfile -File .\start-node.ps1`，使用 `-PythonPath`/`-Workspace` 参数。

启动入口校验包内摘要、创建独立工作目录与虚拟环境、安装该 wheel 和既有依赖，然后启动 API/性能工作节点。不覆盖已有环境，不调整防火墙或系统策略，不把配对令牌/私钥写入启动包；令牌从已有进程环境读取或交互式隐藏输入，仅传入工作节点进程环境。Windows 需要能够获取依赖；安装包和公共 CA 不包含配对令牌值。

如需通过 SSH 远程部署，Windows Hello PIN 不能用于 SSH 密码认证，需使用实际账户密码或公钥；也可在 Windows 本机直接启动节点，无需 SSH。默认 OpenSSH 配置下，管理员账户的公钥放在 `%ProgramData%\ssh\administrators_authorized_keys`，文件权限仅允许 Administrators/SYSTEM；添加公钥保留已有条目，私钥只留在连接端。此 SSH 认证与框架节点的配对令牌是两种独立凭据，参见[微软公钥配置说明](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_keymanagement)。

仅回环地址可用 HTTP；局域网控制服务必须提供 TLS 证书，节点校验证书。Web 控制台可登记节点、查看在线状态和能力、生成/下载 Windows 启动包、启动/停止当前 Web 进程拥有的控制服务；其他进程启动的服务使用 CLI 管理。控制台本身继续仅绑定回环地址。引擎指纹的路径使用统一 `/`，避免 Windows 路径分隔符造成同代码误判；节点同时声明宿主系统与不含明文主机名的主机标识，便于区分本机进程与实际跨机验收。

可使用 `scripts/validate_lan_windows.py --host <Windows-IP> --user <账号> --identity <已有SSH私钥> --report <输出JSON>` 复验；脚本严格检查已有主机密钥，可用 `--known-hosts` 指定已核实的主机记录。需要已有 OpenSSH/Python 3.11+，只向新建 Windows TEMP 工作目录部署，测试目标为脚本在 Mac 创建的受控服务；不操作手机、不新增 SSH 授权、不更改系统策略。Windows 外网下载缓慢时，可加 `--wheelhouse <Windows兼容依赖wheel目录>`，通过局域网传输同批依赖，并仅在启动子进程内设置 pip 离线安装环境；需备齐 Windows 条件依赖与匹配 Python/架构的二进制 wheel。

节点注册协议与源码/资源、声明环境指纹。开始前检查类型、目标来源和凭据输入一致性；两端 testing.environment/default_environment 声明必须一致。性能 spec.account_pool 明确列出每个虚拟用户的 id 和 input_env 映射，数量必须等于 users，不能重复使用等价输入；分布式分配按用户偏移切出不重叠子集。两端预先配置相应环境变量，框架不传送密码值。

任务可自动分发已冻结的 App 包和规范 iOS 测量资产。每块最多 1 MiB，双方复核分块与整体 SHA-256、大小和类型；App 在节点重新检查身份/签名，规范测量复核单位/样本/来源摘要，同 ID 不同内容拒绝覆盖。单 App 最多 1 GiB、任务资产合计最多 2 GiB，不传任意文件、脚本、原始 trace 或设备凭据。设备 profile、原生环境与必要平台工具仍须在节点准备，不自动重配设备。

节点登记可声明 resources，例如 device:shared-lab-phone。同一控制端项目内，不同节点对相同资源互斥；独立控制项目/外部工具不受此声明自动约束。开始前取消会释放，已派发且状态未知则保留 uncertain 占用，断线不自动解锁或重跑。node claims 查看占用；node attempt JOB 查看节点本地已执行结果账本；外部确认进程停止并核查副作用后，node reconcile JOB --stopped --note 给出证据说明，控制台也有对应入口。对账只释放资源并留审计记录，不重放测试。

远程 API 要使用有明确 `allowed_origins` 的类型化请求计划；不远程执行未限定来源的旧 Markdown 用例集合。远程任务不接受任意脚本，也不允许节点继续嵌套分发。跨节点不传递标为敏感的执行输出，含登录凭据的多步骤流程应放在同节点事务/工作流内完成。

性能计划增加 `spec.distribution={"nodes":["worker-a","worker-b"]}` 后，全局速率、并发、阶段和请求预算分摊到节点；每个节点至少分配一个用户，最多 128 用户，全局最多 32 节点/4096 用户。数据命名空间和用户偏移隔离；不会将同一个全局速率复制给每台设备。准备就绪后通过统一起点协调；轮询/网络与时钟带来启动误差，当前不是硬实时同步。

节点持续心跳，失去控制租约会协作式停止本地任务；节点重启、完成回传丢失不会自动重放未知写操作。结果使用有限原始样本计算全局 nearest-rank 分位数，绝不平均节点 P95。节点掉线、缺样、清理失败会保留已收到的证据并判为无法确定。配对节点属于可信执行主机，未提供硬件远程证明。跨独立控制端可配置 [共享资源服务](mobile-advanced.md)：计划顶层 resources、设备资源和节点登记资源统一取得租约，父控制端向隔离进程/工作节点传递授权；失联停止新动作，过期占用保留到原身份明确审计对账，不自动转交。未配置的工具不会自动遵循该服务。

## 版本测试任务

```json
{
  "id": "release-2", "build": {"service_version": "build-2", "environment": "staging"},
  "resources": ["staging:account:test-user"],
  "budget": {"timeout_seconds": 600, "max_requests": 20000},
  "nodes": [
    {"id": "api", "plan_id": "api-smoke"},
    {"id": "ui", "case_id": "nickname-persistence", "case_version": 1, "depends_on": ["api"]},
    {"id": "load", "plan_id": "order-load", "depends_on": ["ui"]},
    {"id": "cleanup", "plan_id": "cleanup-data", "depends_on": ["api"], "always": true}
  ]
}
```

`plan_id` 与 `case_id` 二选一，`case_version` 省略时冻结当前版本；可给节点指定 `node_id` 包装为远程执行。设置 `max_parallel` 可让没有前置依赖的分支并行；节点可声明 `resources`，相同设备/远程节点另有自动互斥。版本任务的全局资源使用预留锁，子节点按同一资源实际占用串行；`resource_wait_seconds` 提供有界等待，默认 0 保留冲突立即失败的行为。仍不支持将工作流再次嵌套进版本工作流，在版本任务中直接列出各计划即可。安装准备通过计划外层配置，随计划冻结。

```bash
iatest test campaign-create release.json --project demo
iatest test campaign-run release-2 --project demo
iatest test campaign-run release-2 --project demo --analyze
iatest test campaigns --project demo
```

同 ID 快照不可覆盖。执行记录绑定快照摘要、构建声明、各节点结果与分类；设备配置发生改变时拒绝运行旧快照。共享资源名称和设备在本宿主用户内持有内核租约；任务退出后释放。业务失败/依赖阻断、环境错误、负载不足、性能门禁、安全控制和未评估项分别保留，不把所有错误合并成业务缺陷。可选 AI 分析生成独立产物，分析失败不改写测试判定。中断写操作不自动续跑，再次显式运行产生新记录。
