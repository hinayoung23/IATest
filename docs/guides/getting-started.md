# 安装与第一个项目

## 安装

需要 Python 3.11 或更新版本。

```sh
python -m pip install iatest-framework
iatest --help
```

源码开发在仓库根目录执行 `python -m pip install -e .`。数据库连接器使用 `pip install 'iatest-framework[database]'`；蓝牙/USB HID 中文转拼音使用 `pip install 'iatest-framework[hid]'`。这些依赖仅在相关功能启用时加载。

## 创建项目

```sh
iatest init --project my-project
iatest web --project my-project
```

访问 `http://127.0.0.1:8765`。顶部导航连接所有功能；右上方项目菜单可切换项目、新建项目、选择语言和主题。控制台只绑定本机回环地址，远程使用需要自行配置带认证的入口。

初始化会创建 `iatest.json`，业务资产保存在 `projects/my-project/`。本地 `.env` 可提供环境变量；密钥使用变量名引用，不写入用例或提交到版本库。

## 配置模型与服务

在控制台的「配置中心」设置主控、用例生成、结果分析模型。未单独配置的角色复用主控模型。模型支持工具或图像输入时需明确声明相应能力。

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
  "environments": {"local": {"base_url": "http://127.0.0.1:8000", "headers": {}}},
  "default_environment": "local"
}
```

配置使用 `iatest.json` 中的现有字段。模型连接测试会请求对应服务；已固化计划的执行不要求逐步调用模型。

## 完成一次 API 测试

```sh
iatest discover ./api-docs/openapi.yaml --project my-project
iatest generate --project my-project
iatest run --project my-project
```

也可在「测试资产 → API 契约」导入文档，在「测试资产 → API 用例」检查或修改用例并执行，最后在「执行与报告 → API 用例报告」查看结果。生成操作会调用模型，执行应针对已授权测试环境。

## 扩展测试类型

```sh
iatest test capabilities
iatest test import-plan examples/plans/web.json --project my-project
iatest test run web-login --project my-project
```

示例文件由源码仓库提供，需要按自己的服务、设备和目标替换参数。浏览器、桌面和移动端需要额外环境，见[环境准备](../reference/environment.md)。完成任务与测试通过分别记录；判定口径见[质量规范](../reference/quality.md)。
