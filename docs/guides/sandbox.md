# 执行隔离与访问策略

默认 `sandbox.mode=policy`。CLI、Web 和定时执行共用运行入口；Web 可以提高本次执行的隔离级别，不能降低配置要求。没有增加 Python 生产依赖。

| 模式 | 适用范围 | 实际边界 |
| --- | --- | --- |
| `policy` | 本地可信项目，默认模式 | 框架入口检查文件和网络访问；仍在主进程执行 |
| `process` | CI、需要强制超时的可信插件 | 独立进程、最小环境、临时工作目录、强制终止和结果检查点；不限制任意 Python 代码的宿主机权限 |
| `container` | 需要限制插件宿主机权限 | 非 root、只读文件系统、受限输入挂载、临时文件与输出配额、CPU/内存/进程限制、受控测试网络 |
| `off` | 本地兼容旧配置时显式选择 | 关闭应用访问策略；插件仍需安装并配置 |

## 配置和运行

在 `iatest.json` 增加或调整：

```json
{
  "sandbox": {
    "mode": "process",
    "read_paths": [],
    "denied_read_paths": ["fixtures/private-credentials.json"],
    "allowed_origins": ["http://127.0.0.1:9100", "https://auth.example.test"],
    "allowed_networks": [],
    "allow_plugins": false,
    "env_allowlist": [],
    "hard_timeout_seconds": 300,
    "termination_grace_seconds": 5,
    "max_output_bytes": 64000000,
    "max_input_bytes": 64000000,
    "max_cases": 10000,
    "memory_mb": 512,
    "cpus": 1,
    "pids_limit": 64
  }
}
```

```bash
iatest run --project demo --isolation process --timeout 120 --no-ai
iatest run --project demo --isolation container --no-ai
iatest fixtures-check --project demo --probe
```

`--isolation` 只覆盖当前 CLI 运行，不修改配置。Web 用例页面可选择使用项目配置、独立进程或容器。`fixtures-check` 在配置为 process/container 时也通过隔离工作进程执行，包括插件加载、自检和 prepare/cleanup。

## 文件边界

请求体 `$iatest_file`、Multipart `file_path`、JSON/CSV 数据集、文档和本地 OpenAPI 外部引用共用文件检查和有界读取。相对执行文件路径以框架项目根目录为基准，与 CLI 启动目录无关。

`read_paths=[]` 默认允许项目根目录中的普通文件；配置非空数组会替换默认读取范围，可填写目录或单个文件的绝对/相对路径。解析符号链接后再检查范围。无论读取范围如何配置，均拒绝 `.env`、`.env.*`、`iatest.json`、`.git`、`.ssh`、`.aws`、`.gnupg`、`.venv`、`.iatest` 和其他项目的文件。可以通过 `denied_read_paths` 增加项目特有的敏感文件。执行环境的 CA、客户端证书和私钥可由 HTTP 客户端加载，但不能作为用例文件上传。

文件读取检查普通文件类型并限制实际读取字节数，避免仅检查 stat 大小后文件增长，也不会阻塞在 FIFO 上。该检查属于应用策略；防御恶意 Python 插件或并发文件替换应使用容器模式。

CLI 明确指定的本地文档可以位于项目外，其同目录引用仍受 OpenAPI 主文档目录限制。Web 文档文件必须位于允许目录；上传文件由框架保存到当前项目目录。

## 网络边界

当前配置的环境 `base_url` 和显式 CLI `--base-url` 自动授权。其他鉴权、夹具、自检地址必须加入 `allowed_origins`。匹配完整协议、主机和端口，不使用域名后缀或通配符；授权端口上的 localhost 和内网服务可以使用。

框架在每次请求和重定向前检查目标。策略开启时不继承系统 HTTP 代理和证书环境变量；显式配置的代理和 TLS 参数仍可使用。CLI 明确指定的主文档 URL 会授权本次读取；Web 文档 URL 必须属于已配置环境地址或 `allowed_origins`。远程 `$ref` 需要同源或额外授权。由文档识别出的接口地址不会自动成为执行授权地址；环境未配置地址时，需要显式授权文档提供的目标。

可选 `allowed_networks` 是 CIDR 数组，例如 `["127.0.0.0/8", "::1/128", "10.20.0.0/16"]`，额外检查目标解析出的所有 IP。HTTP 库后续仍可能再次解析 DNS，这不是防御 DNS 重绑定的完整网络边界。对不可信插件、DNS 或代理，使用容器 internal 网络和受控出口代理/防火墙。

## 工作进程、凭据与中断

主进程负责读取、筛选、数据集展开、项目锁和结果持久化。工作进程只接收本次用例、选定环境、所需夹具及执行设置，不重新加载 `.env`，也不接收 LLM 配置。必要的鉴权/夹具凭据通过权限受限的临时输入传递；其他凭据不会默认继承。插件如确需特定环境变量，可通过 `env_allowlist` 逐项声明。进程模式的 HOME 和临时目录指向本次临时工作目录。

展开用例默认最多 10000 条，总大小由 `max_input_bytes` 限制；工作进程输入包和容器所需文件同样受该配额约束。结果总大小由 `max_output_bytes` 限制。

可信插件需显式设置 `allow_plugins=true`，仍通过已安装包的 `iatest.plugins` entry point 注册。应用策略和普通子进程不能限制插件自己调用 Python 文件或网络 API。运行不可信插件时应使用容器并只提供测试凭据。

每个已完成用例立即保存临时结果检查点。取消或达到硬时限后，先请求协作取消，最多等待 `termination_grace_seconds`，再终止工作进程。需要等待较长的清理逻辑时，应适当增加这个宽限时间。POSIX 使用进程组；Windows 使用 Job Object，并在插件运行前完成启动握手。Linux 进程额外限制地址空间、CPU 时间和单文件大小；Windows Job 限制内存、CPU 时间和活动进程数。macOS 进程模式不保证硬内存限制，严格资源配额使用容器。

主进程保留已完成的结果，未完成用例记录为 cancelled/failed。无法确认环境 teardown 完成时，不将已完成的用例报告为通过；中断运行不会因已有检查点而被判定为整体成功。运行元数据记录隔离模式、命名空间、完成状态、强制终止和中断原因。临时凭据和结果目录在退出后清理。

强制终止不能保证远端 cleanup 成功，报告会记录无法确认清理的状态，不会自动重复可能有副作用的清理操作。

## 容器运行

需要用户已安装并启动 Docker，预先准备包含框架和所需插件的 Linux 镜像。框架使用 `--pull never`，不会下载镜像，也不会在 Docker 不可用时降低隔离级别。

可以使用仓库中的示例构建镜像：

```bash
uv build
docker build -f examples/Dockerfile.sandbox -t iatest-sandbox:local .
```

```json
{
  "sandbox": {
    "mode": "container",
    "container_image": "iatest-sandbox:local",
    "container_network": "none"
  }
}
```

默认 network=none，不允许网络请求。接口服务可加入预先创建的 internal 测试网络：

```bash
docker network create --internal iatest-test
```

将测试服务接入 `iatest-test`，设置 `container_network=iatest-test`，执行环境 base_url 使用测试服务的容器名称。框架会检查网络确为 internal，拒绝 host、bridge 和非 internal 网络。需要访问网络之外的目标时，使用接入 internal 网络的受控出口代理，并配置环境的 `proxy`；出口策略由代理/防火墙实施。自定义网络中的其他服务也是可访问的网络范围，因此该网络应只包含测试服务。

容器只挂载所需输入文件，挂载为只读，不挂载整个项目或 Docker socket。只读根文件系统、移除 capabilities、禁止提权、非 root、内存/CPU/PID 配额均固定启用。工作目录与输出目录使用有容量限制的 tmpfs；输出经过有界协议由主进程写入宿主机，容器没有宿主机可写挂载。Docker 日志持久化关闭，插件标准输出不会进入结果协议。

容器模式文件路径模板只支持用例 variables 和本次 `iatest_namespace`；不能依赖 HTTP 响应提取的本地文件路径。CA/证书单独作为必要输入传入。额外插件数据应通过用例输入或镜像提供，不开放任意宿主机挂载。

## 远端测试数据隔离

每次运行注入唯一的 `{{iatest_namespace}}`，用于夹具准备/清理和用例变量，运行元数据保留该值。例如：

```json
{
  "prepare": {
    "url": "http://127.0.0.1:9100/fixtures/accounts",
    "json": {"namespace": "{{iatest_namespace}}"},
    "extract": {"account_id": "$.account_id"}
  },
  "cleanup": {
    "url": "http://127.0.0.1:9100/fixtures/accounts/{{account_id}}",
    "method": "DELETE",
    "query": {"namespace": "{{iatest_namespace}}"}
  }
}
```

测试服务必须实际使用命名空间隔离数据，并提供幂等清理或 TTL。执行沙箱不会撤销已发送到远端的写请求，不能代替测试环境、独立账号和服务端隔离。

实现参考：[Docker run 官方选项](https://docs.docker.com/reference/cli/docker/container/run/)、[HTTPX 请求事件](https://www.python-httpx.org/advanced/event-hooks/)、[Windows Job Object](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject)。
