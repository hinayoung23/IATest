# 浏览器测试

## 选择后端

| `spec.browser` | 执行方式 | 外部条件 |
|---|---|---|
| `chromium` | 自有 CDP 客户端 | `IATEST_CHROMIUM_PATH` |
| `firefox` | 自有 W3C WebDriver 客户端 | Firefox 与 geckodriver，或明确的回环 WebDriver |
| `webkit` | macOS Safari / safaridriver | 用户启用远程自动化；常规模式需显式选择 |

框架创建独立浏览器配置，不接管个人标签页或登录态。生产执行不依赖 Playwright/Selenium，不下载浏览器。

## 目标与输入

Web 计划声明 `start_url`、`allowed_origins` 和 `steps`。目标支持 id、test_id、label、精确文本、role/name、selector，以及显式 scope 和业务行锚点。

```json
{"selector":"button.details","scope":"#orders","anchor":{"selector":".order-id","text":"order-2","container":"tr"}}
```

`find/click/fill/press/expect/wait/screenshot` 用于基础交互。动作前重新验证目标唯一、可见、连接、启用、编辑、稳定几何和相应命中条件；不使用 JS 修改业务值或 `element.click()` 替代原生输入。

多页面使用明确 page 别名；frame 指定 selector 与授权 origin，Shadow DOM 指定宿主路径。Chromium 支持同源/跨进程 frame、开放/闭合根、平面透视与像素锚点投影。Firefox 支持原生引用与复杂 frame 几何校验；Safari 不宣称具备同等闭合根、不透明 frame 或全部透视视觉能力。

## 文件、滚动和视觉

上传使用授权文件和明确目标；下载需要来源、大小限制，并可使用文件名和 SHA256 门禁。blob/data 下载需明确内容摘要。滚动和 `scroll_find` 使用唯一容器与有限次数；未挂载列表项不能由普通 DOM 查询证明不存在。

局部截图只采集明确目标，视觉入口要求模型显式支持图像。坐标使用前重新核对图像、视口/DPR、节点身份、几何与遮挡；截图不稳定或锚点过期拒绝输入。实际业务结果必须另有确定性断言。

## Safari 常规模式

先启用 Safari 的“允许远程自动化”。若普通 Safari 进程影响启动，保存工作并完全退出，让驱动创建自动化窗口。

```sh
iatest test doctor --browser webkit --network-policy navigation_only
iatest test import-plan examples/plans/safari.json --project demo
iatest test run safari-login --project demo
```

在计划中显式设置：

```json
{"browser":"webkit","network_policy":"navigation_only","start_url":"https://example.com/login","allowed_origins":["https://example.com"],"steps":[{"op":"fill","target":{"id":"name"},"value":"Example"},{"op":"expect","target":{"id":"name"},"assert":"value","expected":"Example"}]}
```

| 策略 | 范围 |
|---|---|
| `strict`（默认） | 要求浏览器确认来源代理能力，按代理规则约束；Safari 当前不具备所需拦截能力，拒绝启动 |
| `navigation_only` | 检查框架导航和定位来源，不拦截页面自身的后台、重定向或子资源请求；不能与要求完整来源/网段约束的上层配置组合 |

常规模式不会自动替代严格模式；没有代理的运行不把被拦截连接数记为零。该模式支持原生输入、断言、截图、明确 frame、开放 Shadow DOM、页面管理与有界文件操作。

## 隔离与判定

浏览器进程/容器 RPC 将原生句柄留在 worker，模型和共享预算留在父端。容器需要已准备的镜像和 Docker，不自动降级。应用代理不等于整个浏览器的 OS 出口隔离，HTTPS 隧道也不提供精确逐请求计数。

任何不完整查询、未知写入或失效上下文不能通过。目标探索见[AI 目标探索](goal-exploration.md)，具体配置见[多类型工作流](test-plans.md)。
