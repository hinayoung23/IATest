# AAPITest

AI 驱动的接口自动化测试框架。通过大模型自动识别接口、生成用例、执行测试、分析报告。

## 安装

```bash
pip install -e .
```

## 快速开始

### 1. 初始化项目

```bash
aitest init
```

### 2. 配置大模型

编辑 `aitest.json`，配置 master / case_generator / analyzer 三个角色：

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

API Key 通过环境变量或 `.env` 文件配置：

```bash
export YOUR_API_KEY_ENV=sk-xxxxxxxx
```

### 3. 配置测试环境

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

### 4. 识别接口

支持 OpenAPI JSON、Markdown、HTML 格式的接口文档：

```bash
aitest discover https://example.com/openapi.json --project my-project
```

### 5. 生成用例

```bash
aitest generate --project my-project
```

框架会自动分析接口依赖、制定覆盖计划、分批生成测试用例。生成策略可在配置中自定义。

### 6. 执行用例

```bash
aitest run --project my-project
```

支持冒烟模式和全量模式，可配置并行数。

### 7. 查看报告

```bash
aitest web --project my-project
```

浏览器访问 `http://127.0.0.1:8765` 查看仪表盘、用例、报告和项目上下文。

## 用例管理

用例以 Markdown 文件存储，每个项目一个文件：

```
projects/<project>/test_cases/<project>.md
```

支持手动编辑，也支持通过 Web 控制台在线编辑。

## 命令行

```bash
aitest init                          # 初始化项目配置
aitest discover <source>             # 导入接口文档
aitest generate --project <name>     # 生成测试用例
aitest run --project <name>          # 执行测试用例
aitest web --project <name>          # 启动 Web 控制台
```

## 断言类型

- `status_code` — HTTP 状态码
- `json_path` — JSON Path 响应字段
- `header` — 响应头
- `body_contains` — 响应体包含指定内容

## License

MIT
