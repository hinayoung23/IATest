# 项目资产与目录

同一工作空间中的每个项目使用独立的 `projects/<project>/`。接口、Web、桌面、Android、iOS、性能、安全等资产都按测试类型保存为可读文件；它们不依赖某个 SQLite 文件才能导出或复用。

## 统一结构

```text
projects/<project>/
├── assets/
│   ├── api/
│   │   ├── documents/                 # 接口契约、来源、元数据
│   │   ├── cases/                     # Markdown 用例、覆盖清单
│   │   └── plans/                     # 类型化 HTTP 计划
│   ├── web/
│   │   ├── plans/<id>.json
│   │   └── cases/<id>/
│   │       ├── goal.json              # AI 目标探索用例及回放证据引用
│   │       └── v000001.json           # 批量导入业务用例的版本记录
│   ├── desktop/                      # plans/、cases/
│   ├── android/
│   │   ├── profiles/<id>.json         # 设备配置
│   │   ├── plans/<id>.json            # 通用测试入口保存的计划
│   │   ├── plans/native/<id>.json     # 原生移动入口保存的计划
│   │   ├── cases/                    # 目标用例、业务用例版本
│   │   └── applications/             # 安装资产的身份、来源和摘要
│   ├── ios/                          # 与 Android 对应的资产分类
│   ├── performance/
│   │   ├── plans/                    # 服务与协议负载计划
│   │   └── client/
│   │       ├── plans/                # 客户端性能计划
│   │       ├── measurements/         # iOS 测量资产
│   │       └── calibrations/         # 校准资产
│   ├── security/
│   │   ├── plans/
│   │   └── rules/                    # 版本化规则库
│   ├── data/plans/
│   ├── protocol/                     # plans/、schemas/（Protobuf）
│   ├── accessibility/                # plans/、reviews/（人工复核）
│   ├── workflow/                     # plans/、campaigns/（版本测试任务）
│   ├── remote/plans/
│   └── shared/                       # 无平台标记或跨类型的资产
├── reports/<type>/<run-id>/
│   ├── result.json
│   └── evidence/                     # 截图等运行证据
├── context/                          # 项目知识的导出和上下文包
└── .iatest/
    ├── indexes/                      # 可重建的资产查询缓存
    ├── state/                        # 内部运行产物与资产排序元数据
    ├── storage-layout.json           # 布局与旧数据迁移标记
    ├── storage-transactions/         # 写入中断时恢复文件的事务记录
    └── …                             # 任务、知识、Agent 状态和二进制缓存
```

目录按实际资产生成文件；初始化会创建测试类型及基本分类的空目录。原生移动运行结果放在 `reports/android|ios/<run-id>/native/`，API 的 Markdown/HTML 导出报告放在 `reports/api/`。客户端性能结果位于 `reports/performance/client/<run-id>/`。

通用与原生移动入口保持各自的计划标识空间，因此都可以拥有同名计划。通用入口中的计划 ID 在一个项目内仍须唯一；不同项目可以使用同名 ID。

## 文件、缓存与版本控制

`assets/` 中的 JSON 和 Markdown 是资产正文的来源。CLI、控制台和应用服务写入同一组文件；SQLite 的内容列是为筛选、关联和版本校验保留的查询缓存，不再是资产唯一副本。读取时会同步新增、修改和删除的文件；格式错误、身份与路径不符或重复 ID 会报错，不会继续使用旧缓存执行。

可以在未执行相关任务时编辑计划文件，保留其 `id`、`kind` 和所在目录的一致性，遵循[计划契约](test-plans.md)。导入用例的版本文件、已固化目标、规则库和测量证据应通过应用入口创建新版本，以保留来源与摘要关系；直接改变已被引用的内容可能使验证失效。不要在文件中填写密码、Token 或设备签名材料，继续使用环境变量引用。

关闭使用该工作空间的 IATest 进程后，删除 `projects/<project>/.iatest/indexes/` 中的缓存数据库，下次访问会从文件重建。不要把 `.iatest/` 整体当作可丢弃缓存：迁移标记、待恢复事务、排序元数据、知识库和任务状态需要保留。

若上次进程异常退出，先正常打开项目完成事务恢复，再关闭进程重建缓存；不要手动删除待恢复的事务目录。

如需版本控制，可在自己的工作空间跟踪 `assets/` 和不含秘密的配置。框架源码仓库默认忽略运行时的 `projects/`，因此 GitHub 源码中不会预放一个业务项目。备份一个完整工作空间时，应停止相关任务并包含项目目录和运行状态；仅复制 `assets/` 可复用测试资产，但不包含完整的历史证据与任务状态。

## 旧结构迁移

正常打开项目时复制旧 `api_docs/`、`test_cases/` 和根层 API 报告到新分类；首次访问通用或原生移动资产时，再从旧 `testing/testing.sqlite3`、`mobile/mobile.sqlite3` 导出相应文件。旧目录和数据库均保留，新操作只更新新结构。

迁移不会覆盖内容不同的目标文件。若发生冲突，错误会指出目标路径；先比较两份内容，将需保留的版本备份或改名，再重新打开项目。迁移完成标记独立于查询缓存，删除缓存不会从旧数据库复活已经删除的新资产。仅通过只读 MCP 查询不会触发写入迁移；它兼容旧路径，迁移后读取新路径。

多文件变更在项目存储锁内提交，并记录提交前后的文件快照。进程中断后，下次访问会按持久提交状态恢复整组文件。旧版进程不会识别新布局，应先退出旧版再升级，避免两个版本同时写同一项目。
