# AAPITest

AI-driven API test automation framework. Auto-discover APIs, generate test cases, execute tests, and analyze results — all powered by LLMs.

AI 驱动的接口自动化测试框架。通过大模型自动识别接口、生成用例、执行测试、分析报告。

## Installation / 安装

```bash
pip install -e .
```

## Quick Start / 快速开始

### 1. Initialize / 初始化

```bash
aitest init
```

### 2. Configure LLM / 配置大模型

Edit `aitest.json` with three roles: master / case_generator / analyzer.

```json
{
  "llm": {
    "master": {
      "enabled": true,
      "provider": "openai-compatible",
      "base_url": "https://api.example.com/v1",
      "model": "your-model",
      "api_key_env": "YOUR_API_KEY_ENV"
    }
  }
}
```

Set API keys via environment variables or `.env`:

```bash
export YOUR_API_KEY_ENV=sk-xxxxxxxx
```

### 3. Configure Environment / 配置测试环境

```json
{
  "environments": {
    "local": {
      "base_url": "http://127.0.0.1:8000",
      "headers": {}
    }
  },
  "default_environment": "local"
}
```

### 4. Discover APIs / 识别接口

Supports OpenAPI JSON, Markdown, and HTML API docs:

```bash
aitest discover https://example.com/openapi.json --project my-project
```

### 5. Generate Cases / 生成用例

```bash
aitest generate --project my-project
```

The framework automatically analyzes API dependencies, builds coverage plans, and generates test cases in batches. Generation strategies are configurable.

框架会自动分析接口依赖、制定覆盖计划、分批生成测试用例。

### 6. Run Tests / 执行用例

```bash
aitest run --project my-project
```

Supports smoke and full modes with configurable parallelism.

### 7. View Reports / 查看报告

```bash
aitest web --project my-project
```

Open `http://127.0.0.1:8765` for the dashboard, cases, reports, and project context.

## CLI / 命令行

```bash
aitest init                          # Initialize project
aitest discover <source>             # Import API docs
aitest generate --project <name>     # Generate test cases
aitest run --project <name>          # Execute test cases
aitest web --project <name>          # Start web console
```

## Assertions / 断言类型

- `status_code` — HTTP status code
- `json_path` — JSON Path field assertions
- `header` — Response header checks
- `body_contains` — Response body content matching

## License

MIT
