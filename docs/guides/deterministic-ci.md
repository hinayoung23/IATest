# 无模型运行与 CI 门禁

保存的确定性计划重跑不需要 LLM。以下示例在源码目录执行；也可以在独立工作空间导入计划文件的绝对路径。

## 数据检查

```sh
iatest init --project demo
iatest test import-plan examples/plans/data-quality.json --project demo
iatest test run data-quality --project demo
```

计划检查两条记录的唯一性与条数，不访问外部服务。修改数据产生重复 ID 后会失败，不会调用模型解释或改变判定。

## API 状态检查

先在另一终端启动标准库演示服务：

```sh
python examples/api_service.py
```

导入并运行明确要求 HTTP 200 的计划：

```sh
iatest test import-plan examples/plans/api-health.json --project demo
iatest test run api-health-status --project demo
```

这是状态码门禁；需要响应字段、Schema 或业务状态断言时，选择协议检查或相应工作流，不能把 HTTP 200 当作业务结果的充分证明。

## 浏览器检查

安装可用的 Chromium 或配置受支持的驱动。在另一终端启动已有界面演示：

```sh
python -c "from examples.ui_goal_lab import create_server; create_server(8766).serve_forever()"
```

运行保存的浏览器断言：

```sh
iatest test import-plan examples/plans/web-ready.json --project demo
iatest test run web-ready --project demo
```

该计划仅验证页面初始 Ready 状态，不修改服务端数据。真实保存/刷新业务用例需要明确的预期、独立数据或清理计划；新浏览器会话不等于服务端数据已清空。AI 探索的来源引文也不能替代业务人员对验收条件语义的确认。

## 门禁与支持范围

`iatest test run` 只有 lifecycle=completed 且 verdict=passed 时返回成功退出码；失败、缺测、取消和环境异常都不能通过。Web/桌面/数据/安全/客户端性能结果包含检查计划数、已评估数和失败数；非法的空检查或缺失断言计划会被拒绝。

API/data 的确定性路径适合先在目标 CI 环境验证。浏览器、桌面、移动、远程节点和性能采集分别需要其环境/设备/部署验收；单个操作系统的单元测试不能证明全部后端兼容。HID、特定 Wayland 与 Apple 原始采集继续遵守[能力矩阵](../reference/capabilities.md)的条件和实验边界。

CI 凭据应只具备测试目标所需权限，并通过受信任的项目 sandbox 策略收窄网络目标。安全扫描范围不能只由导入计划决定；授权工单或归属声明是组织记录，不能代替实际授权。生产策略需要隔离时，不允许执行方把 sandbox 切为 off；运行报告记录实际隔离模式供门禁核对。

## 开发质量与基准

CI/发布使用受维护的 `uv.lock` 和带 hash 的 `requirements/ci.txt`，基础 wheel 验证使用 `requirements/base.txt`。库本身继续声明兼容范围，使用者可以采用自己的依赖政策。更新依赖时重新解析并提交锁定文件及两份导出文件。

```sh
uv lock
uv export --locked --all-extras --no-emit-project -o requirements/ci.txt
uv export --locked --no-emit-project -o requirements/base.txt
python -m ruff check .
python -m mypy
python -m coverage run -m pytest -q tests/test_release_hardening.py tests/test_browser_startup.py tests/test_delivery_hardening.py tests/test_safari_policy.py
python -m coverage report
python scripts/benchmark_core.py --output benchmark.json
```

类型检查覆盖新增关键边界模块，分支覆盖率门禁覆盖连接策略、配置 schema、请求安全、判定汇总与共享等待等模块，当前阈值 85%；不是全库覆盖率声明。基准测量计划校验和 100/1000 个资产读取的 p50/p95，保存系统与 Python 信息。使用相同环境的历史输出通过 `--baseline` 比较，`--max-regression` 指定允许比例；不要把不同硬件的耗时直接作门禁。
