# IATest

AI-driven API test automation for discovering APIs, generating test cases, running tests, reviewing results, and managing reusable project knowledge.

AI 驱动的接口自动化测试框架，用于识别接口、生成用例、执行测试、分析报告和管理项目经验。

[English](#english) | [中文](#中文)

[Release 1.1.0 / 1.1.0 发布说明](docs/releases/1.1.0.md)

[CI execution, datasets, authentication, polling and plugins / 框架补充与使用说明](docs/framework-improvements.md)

[Runtime data, agent evaluation, recovery and MCP / 运行时数据、Agent 评测、恢复与 MCP](docs/agent-data-evolution.md)

[Provider-independent token optimization / 模型无关的 Token 优化与用量统计](docs/agent-data-evolution.md#模型无关的-token-优化)

---

## English

### What you can do

- Import OpenAPI 3.x, Swagger 2.0, Markdown, or HTML API documentation.
- Generate and maintain API test cases.
- Run smoke, full, or selected test cases.
- View reports and recent execution trends.
- Use the local Web console in English, Simplified Chinese, Japanese, French, or Russian.
- Connect project-provided test fixtures and run a local dependency Mock server.

### Requirements

- Python 3.11 or later

### Install

Install the latest release from PyPI:

```bash
python -m pip install iatest-framework
```

For source development, clone the repository and run `python -m pip install -e .` from its root directory.

Confirm that the command is available:

```bash
iatest --help
```

### Quick start

#### 1. Initialize a project

```bash
iatest init --project my-project
```

This creates `iatest.json` and the project directories under `projects/my-project/`.

#### 2. Configure an LLM and the test environment

Edit `iatest.json`:

```json
{
  "llm": {
    "master": {
      "enabled": true,
      "provider": "openai-compatible",
      "base_url": "https://llm.example.com/v1",
      "model": "your-model",
      "api_key_env": "IATEST_LLM_API_KEY",
      "supports_tools": true
    }
  },
  "environments": {
    "local": {
      "base_url": "http://127.0.0.1:8000",
      "headers": {}
    }
  },
  "default_environment": "local"
}
```

Set the API key in your shell or in a local `.env` file:

```bash
export IATEST_LLM_API_KEY=your-api-key
```

`case_generator` and `analyzer` are optional. If they are not configured, IATest reuses `master`.

You can also edit and test all three model connections from **Settings → LLM Configuration** in the Web console.

#### 3. Choose the CLI artifact language

New CLI-generated artifacts use English by default.

```bash
iatest language
iatest language fr
```

Supported codes are `en`, `zh`, `ja`, `fr`, and `ru`. Changing the language only affects new test cases, experiences, and reports. Existing content is not translated.

Operations started from the Web console use the current Web interface language instead of the CLI setting.

#### 4. Import API documentation

From a local file:

```bash
iatest discover ./api-docs/openapi.yaml --project my-project
```

From a URL:

```bash
iatest discover https://example.com/openapi.json --project my-project
```

#### 5. Generate test cases

```bash
iatest generate --project my-project
```

Force regeneration instead of reusing a valid cache:

```bash
iatest generate --project my-project --force
```

#### 6. Run test cases

Run all applicable cases:

```bash
iatest run --project my-project
```

Run selected case IDs:

```bash
iatest run --project my-project --cases TC-API-0001,TC-API-0002
```

Temporarily override the configured target base URL:

```bash
iatest run --project my-project --base-url http://127.0.0.1:9000
```

#### 7. Start the Web console

```bash
iatest web --project my-project
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765).

The Web console provides:

- Dashboard and project switching.
- API source import and API editing.
- Test-case generation, supplemental generation, editing, execution, and scheduling.
- Per-item and bulk deletion for test cases and reports.
- Report details and execution trends.
- Project knowledge review, search, confirmation, ignore, keep, and merge actions.
- LLM, generation strategy, execution strategy, fixture, Mock, and governance settings.
- A language selector for English, Chinese, Japanese, French, and Russian.

### External test capabilities

Some documented scenarios need capabilities that belong to the tested project, such as server-time control, controlled email delivery, or reserved test data. IATest does not create these capabilities automatically.

If a required capability is not connected, the case remains visible for coverage but is recorded as `skipped`. Connect it in `iatest.json` or from **Settings → Project-provided Test Capabilities**.

Static value provider:

```json
{
  "fixtures": {
    "providers": {
      "restricted_prefix": {
        "enabled": true,
        "type": "static",
        "values": {
          "prefix": "admin"
        }
      }
    }
  }
}
```

HTTP prepare and cleanup provider:

```json
{
  "fixtures": {
    "timeout_seconds": 10,
    "providers": {
      "mail_injection": {
        "enabled": true,
        "type": "http",
        "prepare": {
          "method": "POST",
          "url": "http://127.0.0.1:9000/test-support/mail",
          "json": {"email": "{{email}}"},
          "expected_status": 201,
          "extract": {"fixture_token": "$.fixture_token"}
        },
        "cleanup": {
          "method": "POST",
          "url": "http://127.0.0.1:9000/test-support/mail/cleanup",
          "json": {"token": "{{fixture_token}}"},
          "expected_status": 204
        }
      }
    }
  }
}
```

Fixture preparation and cleanup are deterministic local operations and do not call an LLM.

### Dependency Mock server

The Mock server is intended for upstream dependencies of the system under test. It does not replace the tested API itself.

Example configuration:

```json
{
  "mock": {
    "host": "127.0.0.1",
    "port": 8780,
    "max_request_bytes": 1000000,
    "max_history": 200,
    "routes": [
      {
        "name": "upstream-user-error",
        "method": "GET",
        "path": "/upstream/users/{user_id}",
        "match": {"query": {"mode": "failure"}},
        "response": {
          "status": 503,
          "headers": {"X-Mock": "iatest"},
          "body": {"user": "{{user_id}}", "error": "upstream unavailable"}
        }
      }
    ]
  }
}
```

Start it from the terminal:

```bash
iatest mock --project my-project
```

You can also save the configuration and turn the listening port on or off from **Settings → API Mock**. The Mock listener is off by default and stops when the Web process exits.

### Common commands

```bash
iatest init --project <name>
iatest discover <source> --project <name>
iatest generate --project <name>
iatest run --project <name>
iatest web --project <name>
iatest mock --project <name>
iatest language <en|zh|ja|fr|ru>
iatest llm-check --base-url <url> --model <model>
iatest agent-runs --project <name>
iatest maintain --project <name> --source <source> --ai-update --write
iatest context stats --project <name>
iatest context govern --project <name>
iatest context export --project <name>
iatest context import <file.jsonl> --project <name>
```

Use `iatest <command> --help` for all options.

### Project output locations

```text
projects/<project>/api_docs/       Imported API information
projects/<project>/test_cases/     Generated test cases
projects/<project>/reports/        Test reports
projects/<project>/context/        Context exports and audit snapshots
projects/<project>/.iatest/        Local runtime databases and caches
```

Do not commit `.env` or plaintext API keys.

---

## 中文

### 可以完成什么

- 导入 OpenAPI 3.x、Swagger 2.0、Markdown 或 HTML 接口文档。
- 生成、补充和维护接口测试用例。
- 执行冒烟、全量或指定用例。
- 查看测试报告和最近执行趋势。
- 使用支持英文、简体中文、日文、法文和俄文的本地 Web 控制台。
- 接入项目方提供的外部测试能力，并启动本地依赖 Mock 服务。

### 环境要求

- Python 3.11 或更高版本

### 安装

从 PyPI 安装最新版本：

```bash
python -m pip install iatest-framework
```

如需参与源码开发，请克隆仓库，并在仓库根目录执行 `python -m pip install -e .`。

确认命令已经可用：

```bash
iatest --help
```

### 快速开始

#### 1. 初始化项目

```bash
iatest init --project my-project
```

该命令会创建 `iatest.json`，并在 `projects/my-project/` 下创建项目目录。

#### 2. 配置大模型和测试环境

编辑 `iatest.json`：

```json
{
  "llm": {
    "master": {
      "enabled": true,
      "provider": "openai-compatible",
      "base_url": "https://llm.example.com/v1",
      "model": "your-model",
      "api_key_env": "IATEST_LLM_API_KEY",
      "supports_tools": true
    }
  },
  "environments": {
    "local": {
      "base_url": "http://127.0.0.1:8000",
      "headers": {}
    }
  },
  "default_environment": "local"
}
```

在终端环境变量或本地 `.env` 文件中设置 API Key：

```bash
export IATEST_LLM_API_KEY=your-api-key
```

`case_generator` 和 `analyzer` 是可选配置；没有单独配置时会复用 `master`。

也可以在 Web 控制台的“配置中心 → 大模型配置”中修改三个模型连接并测试连通性。

#### 3. 选择终端产物语言

终端新生成的产物默认使用英文。

```bash
iatest language
iatest language zh
```

支持 `en`、`zh`、`ja`、`fr`、`ru`。切换后只影响新生成的用例、经验和报告，已有内容不会被翻译。

从 Web 控制台发起的操作以当前 Web 页面语言为准，不使用终端语言设置。

#### 4. 导入接口文档

导入本地文件：

```bash
iatest discover ./api-docs/openapi.yaml --project my-project
```

导入在线文档：

```bash
iatest discover https://example.com/openapi.json --project my-project
```

#### 5. 生成测试用例

```bash
iatest generate --project my-project
```

忽略有效缓存并强制重新生成：

```bash
iatest generate --project my-project --force
```

#### 6. 执行测试用例

执行当前范围内的全部适用用例：

```bash
iatest run --project my-project
```

执行指定用例：

```bash
iatest run --project my-project --cases TC-API-0001,TC-API-0002
```

临时覆盖配置中的被测接口基础地址：

```bash
iatest run --project my-project --base-url http://127.0.0.1:9000
```

#### 7. 启动 Web 控制台

```bash
iatest web --project my-project
```

浏览器打开 [http://127.0.0.1:8765](http://127.0.0.1:8765)。

Web 控制台提供：

- 仪表盘和项目切换。
- 接口来源导入以及接口新增、编辑和删除。
- 用例生成、补充、编辑、执行和定时任务。
- 用例和报告的单条删除、勾选多条批量删除。
- 报告详情和执行趋势。
- 项目经验搜索、确认、忽略、保留和合并。
- 大模型、生成策略、执行策略、外部能力、Mock 和治理配置。
- 英文、中文、日文、法文和俄文切换。

### 接入项目方外部测试能力

部分文档场景依赖被测项目提供的服务端时间控制、受控邮件投递或特殊测试数据。IATest 不会自动创建这些能力。

能力尚未接入时，用例仍会保留以体现覆盖情况，但执行结果记录为 `skipped`。可以修改 `iatest.json`，或在“配置中心 → 项目方外部测试能力”中接入。

静态变量提供者：

```json
{
  "fixtures": {
    "providers": {
      "restricted_prefix": {
        "enabled": true,
        "type": "static",
        "values": {
          "prefix": "admin"
        }
      }
    }
  }
}
```

HTTP 准备与清理提供者：

```json
{
  "fixtures": {
    "timeout_seconds": 10,
    "providers": {
      "mail_injection": {
        "enabled": true,
        "type": "http",
        "prepare": {
          "method": "POST",
          "url": "http://127.0.0.1:9000/test-support/mail",
          "json": {"email": "{{email}}"},
          "expected_status": 201,
          "extract": {"fixture_token": "$.fixture_token"}
        },
        "cleanup": {
          "method": "POST",
          "url": "http://127.0.0.1:9000/test-support/mail/cleanup",
          "json": {"token": "{{fixture_token}}"},
          "expected_status": 204
        }
      }
    }
  }
}
```

外部能力的准备和清理都是本地确定性操作，不会调用大模型。

### 接口 Mock

Mock 服务用于模拟被测系统依赖的上游服务，不能用来替代被测接口本身。

配置示例：

```json
{
  "mock": {
    "host": "127.0.0.1",
    "port": 8780,
    "max_request_bytes": 1000000,
    "max_history": 200,
    "routes": [
      {
        "name": "upstream-user-error",
        "method": "GET",
        "path": "/upstream/users/{user_id}",
        "match": {"query": {"mode": "failure"}},
        "response": {
          "status": 503,
          "headers": {"X-Mock": "iatest"},
          "body": {"user": "{{user_id}}", "error": "upstream unavailable"}
        }
      }
    ]
  }
}
```

通过终端启动：

```bash
iatest mock --project my-project
```

也可以在“配置中心 → 接口 Mock”中保存配置并开启或关闭监听。Mock 监听默认关闭，并随 Web 进程退出而停止。

### 常用命令

```bash
iatest init --project <name>
iatest discover <source> --project <name>
iatest generate --project <name>
iatest run --project <name>
iatest web --project <name>
iatest mock --project <name>
iatest language <en|zh|ja|fr|ru>
iatest llm-check --base-url <url> --model <model>
iatest agent-runs --project <name>
iatest maintain --project <name> --source <source> --ai-update --write
iatest context stats --project <name>
iatest context govern --project <name>
iatest context export --project <name>
iatest context import <file.jsonl> --project <name>
```

使用 `iatest <command> --help` 查看完整参数。

执行默认启用文件和网络访问策略；可用 `iatest run --isolation process` 启用独立进程和强制超时。Web 同样支持选择执行隔离；容器模式、访问白名单和插件配置见 [执行隔离说明](docs/sandbox.md)。

### 项目产物位置

```text
projects/<project>/api_docs/       接口识别结果
projects/<project>/test_cases/     测试用例
projects/<project>/reports/        测试报告
projects/<project>/context/        上下文导出和审计快照
projects/<project>/.iatest/        本地运行数据库和缓存
```

不要提交 `.env` 或明文 API Key。

## License

Apache-2.0
