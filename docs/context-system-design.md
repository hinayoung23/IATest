# AI 接口自动化框架 — 上下文系统设计方案（v2）

> 交付状态（2026-07）：SQLite/FTS5 分页检索、万级候选预筛、严格确认/依赖验证门槛、ContextPack 自动保留与文件对账、操作错误审计均已落地。合法的旧版 pack JSON 会先回填数据库，缺失镜像会从数据库修复，避免升级时直接丢失审计历史。

## 一、设计目标

框架作为通用产品，目标规模：

| 维度 | 设计上限 |
|------|----------|
| 项目数 | 50+ |
| 单项目接口数 | 500+ |
| 单项目用例数 | 5000+ |
| 单项目 lessons 积累 | 10000+ |
| 单项目历史报告 | 2000+ |
| 并发场景 | 单机多线程（Web + 后台任务 + 定时执行） |

核心原则：

1. **上下文数据按项目物理隔离**：每个项目独立的 `context.db`，数据库文件路径为 `projects/<project>/.iatest/context.db`，与框架现有的 `results.sqlite3` 隔离模式一致。
2. **AI 提取的所有内容默认 lifecycle_status=pending，人工确认后才能变为 active。**
3. **只有经过执行验证的事实才能进入 verification_status=verified。**
4. **SQLite 为主运行时存储，JSONL 为导入/导出/审计备份格式。**
5. **提取失败绝不能影响主流程。**
6. **上下文注入必须有硬 token 上限，基于模型实际窗口动态计算。**
7. **结论冲突的 lessons 不能自动合并，必须进入冲突裁决。**
8. **跨项目数据交换通过 JSONL 导出/导入完成，不走跨库查询。**

---

## 二、存储架构

### 2.1 项目隔离

框架已有按项目隔离的存储模式（执行结果 `projects/<project>/.iatest/results.sqlite3`）。上下文系统遵循相同模式，**每个项目拥有独立的上下文数据库**：

```
projects/
  sample-api/
    .iatest/
      results.sqlite3       # 现有的执行结果存储
      context.db            # 项目独立的上下文数据库
    context/
      exports/              # JSONL 导出
      packs/                # ContextPack 审计快照
  another-project/
    .iatest/
      context.db            # 完全隔离，互不影响
    context/
      ...
```

**设计要点**：
- **数据库级隔离**：不同项目的 lessons、patterns、packs 完全物理隔离
- **`project` 字段作为二次保险**：核心表仍保留 `project` 列，防止跨项目数据混入，同时让 JSONL 导出可自描述
- **跨项目聚合**：未来如果需要跨项目视图（如"所有项目上下文健康度总览"），通过读取多个 `context.db` 在应用层聚合，不走跨库 JOIN

### 2.2 运行时 vs 持久化

```
┌─────────────────────────────────────────────────┐
│                  运行时 (内存)                     │
│  ┌──────────────┐  ┌──────────────────────────┐ │
│  │ Index Cache  │  │ ContextPack (单次调用)     │ │
│  │ (启动时加载)  │  │ (请求期间存活)             │ │
│  └──────────────┘  └──────────────────────────┘ │
└──────────────────────┬──────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────┐
│     主存储 SQLite (per-project, 项目隔离)          │
│     projects/<project>/.iatest/context.db        │
│                                                  │
│  lessons              lesson_relations           │
│  lesson_sources       context_packs              │
│  extraction_jobs      audit_logs                │
│  execution_trends     failure_patterns           │
│  schema_version                                  │
│                                                  │
│  能力：并发读写 · 条件查询 · 分页 · 全文搜索(FTS5) │
│        · 事务 · 乐观锁 · 统计分析 · 数据迁移       │
└──────────────────────┬──────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────┐
│        便携格式 JSONL / JSON (导入/导出/备份)      │
│                                                  │
│  projects/<project>/context/exports/             │
│    lessons-20260617.jsonl                        │
│  projects/<project>/context/packs/               │
│    case-generation-xxx.json                      │
│    report-analysis-xxx.json                      │
└─────────────────────────────────────────────────┘
```

**设计要点**：
- SQLite 是唯一运行时写入入口，保证并发安全和事务一致性
- 每个项目启动时从自己的 `context.db` 构建内存索引
- JSONL 导出带 `project` 字段，导入时可指定目标项目（合并模式）

### 2.2 数据库表结构

```sql
-- 核心经验表
CREATE TABLE lessons (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,            -- 所属项目名（与 context.db 所在项目目录对应）
    schema_version INTEGER NOT NULL DEFAULT 1,
    revision INTEGER NOT NULL DEFAULT 1,

    -- 分层
    scope TEXT NOT NULL,              -- endpoint | project | case | report | dependency
    method TEXT,
    path TEXT,

    -- 分类
    lesson_type TEXT NOT NULL,        -- business_rule | assertion_pattern | failure_pattern
                                      -- | data_constraint | known_issue | data_dependency
                                      -- | fix_suggestion | trend_insight
    lesson_subtype TEXT,

    -- 环境
    environment TEXT DEFAULT '',      -- test | staging | prod | ''
    base_url_hash TEXT DEFAULT '',

    -- 内容
    title TEXT NOT NULL,
    content TEXT NOT NULL,

    -- 去重签名
    exact_key TEXT NOT NULL,
    semantic_key TEXT NOT NULL,

    -- 三态拆分
    lifecycle_status TEXT NOT NULL DEFAULT 'pending',
        -- pending | active | ignored | resolved | archived | merged
    review_status TEXT NOT NULL DEFAULT 'unreviewed',
        -- unreviewed | confirmed | rejected | needs_review
    verification_status TEXT NOT NULL DEFAULT 'unverified',
        -- unverified | candidate | verified | invalidated

    -- 来源
    source TEXT NOT NULL,             -- 原始报告/用例标识
    source_type TEXT NOT NULL,        -- report_analysis | case_generation | manual_edit
                                      -- | ai_discovery | execution_verified

    -- 置信度与质量
    confidence TEXT NOT NULL DEFAULT 'medium',  -- high | medium | low
    quality_score REAL DEFAULT 0.0,
    quality_reasons TEXT DEFAULT '[]',          -- JSON 数组

    -- 时间线
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    last_seen_source TEXT,
    confirmed_at TEXT,
    expires_at TEXT,

    -- 关联
    tags TEXT DEFAULT '[]',           -- JSON 数组
    related_lesson_ids TEXT DEFAULT '[]',
    merge_candidate_ids TEXT DEFAULT '[]',

    -- 审计
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    created_by TEXT DEFAULT 'system',
    updated_by TEXT DEFAULT 'system',

    -- 元数据
    metadata TEXT DEFAULT '{}'        -- JSON: token_count, priority_score 等
);

CREATE INDEX idx_lessons_project ON lessons(project);
CREATE INDEX idx_lessons_exact_key ON lessons(exact_key);
CREATE INDEX idx_lessons_semantic_key ON lessons(semantic_key);
CREATE INDEX idx_lessons_scope_endpoint ON lessons(project, scope, method, path);
CREATE INDEX idx_lessons_lifecycle ON lessons(lifecycle_status);
CREATE INDEX idx_lessons_verification ON lessons(verification_status);
CREATE INDEX idx_lessons_tags ON lessons(tags);

-- 全文搜索（FTS5）
CREATE VIRTUAL TABLE lessons_fts USING fts5(
    id UNINDEXED,
    title,
    content
);

-- lessons 的 insert/update/delete 触发器同步 FTS5；
-- `iatest context rebuild-index` 可执行全量重建和审计。

-- 注：lesson_relations、lesson_sources 通过 FK(lesson_id) 关联 lessons 表，
--     项目隔离由 lessons.project 保证，这两张表不重复存储 project 字段。

-- Lesson 之间的关系
CREATE TABLE lesson_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id TEXT NOT NULL,
    related_lesson_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,      -- related | similar | conflict | superseded_by
                                      -- | merged_into | dependency_of
    notes TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (lesson_id) REFERENCES lessons(id),
    FOREIGN KEY (related_lesson_id) REFERENCES lessons(id)
);

-- 来源追溯（一条 lesson 可能来自多份报告）
CREATE TABLE lesson_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id TEXT NOT NULL,
    source TEXT NOT NULL,             -- 报告路径 / 用例 ID
    source_type TEXT NOT NULL,
    extracted_at TEXT NOT NULL,
    FOREIGN KEY (lesson_id) REFERENCES lessons(id)
);

-- 上下文注入审计包
CREATE TABLE context_packs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    task_type TEXT NOT NULL,          -- case_generation | report_analysis
                                      -- | api_discovery | case_maintenance
    project TEXT NOT NULL,
    model TEXT NOT NULL,
    model_context_window INTEGER,
    context_budget INTEGER NOT NULL,
    used_tokens INTEGER NOT NULL,
    reserve_output_tokens INTEGER,
    included_lesson_ids TEXT DEFAULT '[]',   -- JSON 数组
    excluded_lesson_ids TEXT DEFAULT '[]',   -- JSON 数组 [{id, reason}]
    final_prompt_hash TEXT,
    created_at TEXT NOT NULL
);

-- 上下文提取任务队列
CREATE TABLE extraction_jobs (
    id TEXT PRIMARY KEY,
    trigger TEXT NOT NULL,            -- report_analysis | case_generation | manual | maintain
    source TEXT NOT NULL,
    project TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | running | failed | permanent_failed | completed
    error TEXT,
    max_retries INTEGER NOT NULL DEFAULT 3,
    retry_count INTEGER NOT NULL DEFAULT 0,
    retry_backoff TEXT NOT NULL DEFAULT 'exponential',
    next_retry_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 审计日志
CREATE TABLE audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,             -- lesson_created | lesson_confirmed | lesson_rejected
                                      -- | lesson_merged | lesson_edited | conflict_resolved
                                      -- | dependency_verified | dependency_invalidated
                                      -- | context_injected | extraction_completed
    entity_type TEXT NOT NULL,        -- lesson | context_pack | dependency | extraction_job
    entity_id TEXT NOT NULL,
    project TEXT NOT NULL,
    actor TEXT DEFAULT 'system',
    detail TEXT DEFAULT '{}',         -- JSON
    created_at TEXT NOT NULL
);

-- 趋势快照（每次执行后保存一份聚合数据）
CREATE TABLE execution_trends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    project TEXT NOT NULL,
    created_at TEXT NOT NULL,

    -- 总体指标
    total_cases INTEGER NOT NULL,
    passed_cases INTEGER NOT NULL,
    failed_cases INTEGER NOT NULL,
    pass_rate REAL NOT NULL,

    -- 按失败类型分布（JSON）
    failure_distribution TEXT NOT NULL DEFAULT '{}',
        -- {"interface_bug": 3, "case_assertion_error": 5, "test_data_issue": 2, "env_issue": 1, "known_issue_recurrence": 2, "new_issue": 1}

    -- 按接口聚合（JSON）
    endpoint_metrics TEXT NOT NULL DEFAULT '[]',
        -- [{"method":"GET","path":"/api/login","passed":5,"failed":0,"avg_ms":120}, ...]

    -- 响应时间分位数（JSON）
    latency_percentiles TEXT NOT NULL DEFAULT '{}',
        -- {"p50": 120, "p90": 350, "p95": 520, "p99": 1200}

    -- 已知问题追踪（JSON）
    known_issue_status TEXT NOT NULL DEFAULT '{}',
        -- {"les-001": {"status":"active","recurred":true}, "les-002": {"status":"resolved_candidate","recurred":false}}

    -- 与上次执行的对比（JSON）
    delta_from_previous TEXT DEFAULT '{}'
        -- {"pass_rate_delta": -0.05, "new_failures": 2, "fixed_failures": 1}
);

-- 失败模式聚合（跨多次执行的汇总视图，定期刷新）
CREATE TABLE failure_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    method TEXT,
    path TEXT,
    failure_category TEXT NOT NULL,     -- interface_bug | case_assertion_error | test_data_issue
                                       -- | env_issue | known_issue_recurrence | new_issue
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    trend_direction TEXT DEFAULT 'stable',  -- improving | worsening | stable | fluctuating
    related_lesson_ids TEXT DEFAULT '[]',
    metadata TEXT DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_failure_patterns_project ON failure_patterns(project);
CREATE INDEX idx_failure_patterns_endpoint ON failure_patterns(method, path);

-- Schema 版本
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    description TEXT
);
```

### 2.3 Schema Version & Migration

```yaml
# iatest.yaml 新增
context:
  schema_version: 2
  auto_migrate: true           # 启动时自动执行未应用的 migration
  backup_before_migrate: true  # 迁移前自动备份
```

CLI 命令：

```bash
iatest context migrate          # 执行待应用的 migration
iatest context validate         # 校验 lessons 完整性
iatest context export           # 导出当前项目 lessons 为 JSONL
iatest context import <file>    # 从 JSONL 导入（合并模式，检查去重）
iatest context rebuild-index    # 从 JSONL 重建 SQLite 数据库
```

每条 lesson 记录 `schema_version` 和 `revision`。migration 脚本按版本号顺序执行，升级旧字段、补充新字段默认值。

---

## 三、核心数据模型

### 3.1 三态拆分

单一 `status` 字段承载太多语义。拆分为三个正交维度：

| 维度 | 字段 | 可选值 | 含义 |
|------|------|--------|------|
| 生命周期 | `lifecycle_status` | pending / active / ignored / resolved / archived / merged | 这条经验当前处于什么阶段 |
| 人工审核 | `review_status` | unreviewed / confirmed / rejected / needs_review | 人是否确认过 |
| 验证状态 | `verification_status` | unverified / candidate / verified / invalidated | 是否经过执行验证 |

**组合示例**：

| lifecycle | review | verification | 场景 |
|-----------|--------|-------------|------|
| active | confirmed | verified | 最佳：人确认 + 执行验证通过 |
| active | confirmed | unverified | 业务规则，无需执行验证 |
| active | unreviewed | candidate | AI 推断的依赖，等待验证 |
| active | confirmed | candidate | 人确认但尚未执行验证的依赖 |
| resolved | confirmed | verified | 已解决问题 |
| pending | unreviewed | unverified | AI 刚提取，未处理 |
| active | needs_review | invalidated | 接口变更导致经验失效，需复核 |

### 3.2 Lesson 关系类型

```
┌──────────────────────────────────────────────────────┐
│                   Lesson 关系                         │
│                                                      │
│  duplicate     完全重复 → 自动合并 occurrence_count   │
│  similar       相似但不完全相同 → 进入待合并队列        │
│  conflict      结论冲突 → 进入待裁决（不可自动合并）     │
│  superseded_by 被新经验替代 → 旧经验降级              │
│  merged_into   已被合并到 → 标记为 merged             │
│  dependency_of 数据依赖关系 → A 是 B 的依赖           │
└──────────────────────────────────────────────────────┘
```

**冲突裁决**：当两条 active lesson 对同一接口做出矛盾断言时（如「返回 200」vs「返回 404」），Web 页面展示冲突双方、各自的来源报告、最近复现时间、置信度，由人选择：
- 保留 A，B 标记为 rejected
- 保留 B，A 标记为 rejected
- 都保留，标记为环境差异（设置不同 environment）
- 都忽略，等更多数据

### 3.3 完整 Lesson 结构

```json
{
  "id": "les-20260617-a1b2c3",
  "schema_version": 2,
  "revision": 3,

  "scope": "endpoint",
  "method": "GET",
  "path": "/api/check/{email}",

  "lesson_type": "business_rule",
  "lesson_subtype": "response_pattern",

  "environment": "test",
  "base_url_hash": "a1b2c3d4",

  "title": "邮箱不存在时接口仍返回 200",
  "content": "邮箱不存在或已过期时接口返回 HTTP 200，响应体 valid=false、expires_in=0。不应预期 404，不应使用 status_code!=200 作为失败断言。",

  "exact_key": "endpoint:GET:/api/check/{email}:business_rule:a1b2c3d4e5f6",
  "semantic_key": "business_rule:response_pattern:status_code:200_valid_false",

  "lifecycle_status": "active",
  "review_status": "confirmed",
  "verification_status": "verified",

  "source": "report-20260616-135848.md",
  "source_type": "report_analysis",

  "confidence": "high",
  "quality_score": 0.94,
  "quality_reasons": ["confirmed_by_user", "seen_5_times", "verified_by_execution"],

  "occurrence_count": 5,
  "first_seen": "2026-06-14T22:00:00",
  "last_seen": "2026-06-17T10:20:00",
  "last_seen_source": "report-20260617-101200.md",
  "confirmed_at": "2026-06-17T10:00:00",
  "expires_at": null,

  "tags": ["assertion_pattern", "negative_case", "status_code"],
  "related_lesson_ids": ["les-20260614-x1y2z3"],
  "merge_candidate_ids": [],

  "created_at": "2026-06-16T14:00:00",
  "updated_at": "2026-06-17T10:20:00",
  "created_by": "system",
  "updated_by": "user:wzl",

  "metadata": {
    "token_count": 92,
    "priority_score": 0.94
  }
}
```

---

## 四、去重、合并与冲突裁决

### 4.1 三层处理

```
新 lesson 提取
  ↓
计算 exact_key = scope:method:path:lesson_type:normalized_claim_hash
  ↓
┌─ 匹配已有 active lesson（同 exact_key）
│   → duplicate：自动合并
│     occurrence_count += 1, last_seen = now
│     lesson_sources 追加新来源
│     不创建新记录
│
├─ 无 exact_key 匹配，但 semantic_key 匹配
│   → similar：进入等待合并队列
│     新 lesson 创建（lifecycle_status=pending）
│     lesson_relations 记录 similar 关系
│     Web 页面展示"相似经验待合并"
│
├─ exact_key 匹配但 content 矛盾（同接口同类型但结论相反）
│   → conflict：进入冲突裁决
│     新 lesson 创建（lifecycle_status=pending）
│     lesson_relations 记录 conflict 关系
│     已有 active lesson 标记 review_status=needs_review
│     Web 页面突出展示冲突，等待人工裁决
│
└─ 无任何匹配
    → 直接创建（lifecycle_status=pending）
```

### 4.2 签名计算

```python
def compute_exact_key(lesson: dict) -> str:
    """用于强去重和冲突检测"""
    normalized = normalize_claim(lesson["content"])
    # 去数字、日期、变量值、URL，取核心主张句
    claim_hash = hashlib.md5(normalized.encode()).hexdigest()[:12]
    env = lesson.get("environment", "")
    return f"{lesson['scope']}:{lesson.get('method','')}:{lesson.get('path','')}:{lesson['lesson_type']}:{env}:{claim_hash}"

def compute_semantic_key(lesson: dict) -> str:
    """用于相似检测"""
    return f"{lesson['lesson_type']}:{lesson.get('lesson_subtype','')}:{extract_subject(lesson)}:{extract_predicate(lesson)}:{extract_object(lesson)}"

def detect_conflict(new_lesson: dict, existing_lesson: dict) -> bool:
    """同 exact_key 但结论相反 → 冲突"""
    same = (new_lesson["exact_key"] == existing_lesson["exact_key"])
    contradictory = is_contradictory(new_lesson["content"], existing_lesson["content"])
    return same and contradictory
```

---

## 五、Token 预算与注入策略

### 5.1 模型能力感知

LLM 配置中增加模型能力字段：

```json
{
  "master": {
    "provider": "anthropic",
    "model": "claude-opus-4-8",
    "context_window": 200000,
    "default_output_tokens": 8000,
    "supports_stream": true
  }
}
```

### 5.2 动态预算计算

```python
def calculate_injection_budget(model_config: dict, task_type: str, system_prompt: str, user_prompt_base: str) -> int:
    ratios = {
        "api_discovery": 0.15,
        "case_generation": 0.25,
        "report_analysis": 0.30,
        "case_maintenance": 0.20,
    }
    caps = {
        "api_discovery": 8000,
        "case_generation": 12000,
        "report_analysis": 16000,
        "case_maintenance": 10000,
    }

    context_window = model_config.get("context_window", 128000)
    reserve_output = task_budget.get(
        "reserve_output_tokens",
        model_config.get("default_output_tokens", 8000),
    )
    safety_margin = 1000  # 计入 tokenizer 误差

    # 系统 prompt + 基础 user prompt 的 token 估算
    system_tokens = estimate_tokens(system_prompt)
    base_user_tokens = estimate_tokens(user_prompt_base)

    available = context_window - system_tokens - base_user_tokens - reserve_output - safety_margin
    budget_by_ratio = int(available * ratios.get(task_type, 0.20))
    budget = min(budget_by_ratio, caps.get(task_type, 12000))

    return max(budget, 0)
```

### 5.3 确定性排序链

```
检索候选 lesson（从 SQLite 按 endpoint/tag/scope 查）
  ↓
排序（优先级从高到低）：
  1. 当前接口精确匹配（同 method + path + environment）
  2. 同 tag / 同模块（path 前缀匹配）
  3. 项目级通用规则（scope=project, lifecycle_status=active）
  4. 最近失败相关（source 为近期失败报告）
  5. review_status=confirmed + verification_status=verified
  6. 高置信度 + 高 quality_score
  7. lifecycle_status=active 但 review_status=unreviewed
  ↓
污染防护过滤（以下不注入）：
  - lifecycle_status=pending（未经确认）
  - confidence=low（低质量）
  - review_status=rejected（被拒绝）
  - lifecycle_status=archived 或 merged（已归档/合并）
  - 存在 active conflict 关系的 lesson（结论矛盾待裁决）
  - 接口文档变更后 review_status=needs_review 的 lesson
  ↓
同一优先级内按 quality_score 降序 → occurrence_count 降序
  ↓
累计 token 超过 budget → 后续 lesson 摘要化（只保留 title + 一句话结论）
  ↓
摘要后仍超 → 截断，记录到 context_pack.excluded_lesson_ids (reason: over_budget)
```

### 5.4 注入 Prompt 模版

用例生成时，在 user prompt 中增加：

```
## 项目历史上下文

### 项目级规则（已确认）
{{PROJECT_RULES}}

### 当前接口经验
{{ENDPOINT_LESSONS}}

### 已知问题（当前未解决，注意复现路径）
{{KNOWN_ISSUES}}

### 历史断言问题（避免重复错误）
{{CASE_LESSONS}}

要求：
- lifecycle_status=active + review_status=confirmed 的经验必须优先于通用策略。
- 不要重复生成历史上已确认错误的断言模式。
- 如果已知问题对应接口，负向用例应体现已知问题的复现路径。
- 如果环境不匹配（如 prod 经验用于 test），标注为"参考"而非"强约束"。
```

### 5.5 环境匹配规则

注入时按环境匹配优先级：

```
同 environment 的 lesson → 强约束（必须遵守）
environment 为空的 lesson → 通用约束（默认适用）
不同 environment 的 lesson → 参考（仅供参考，标注差异）
```

### 5.6 Agent 任务内记忆与自动恢复

经验库注入解决“跨任务应该记住什么”，Agent 任务记忆解决“同一任务进行到哪里”。两者不能混成不断增长的
消息历史。单次 Agent 运行维护以下状态：

| 状态 | Prompt 中的表示 | 持久化位置 |
|---|---|---|
| 原始任务目标 | 短任务保留全文，长任务保留首尾、哈希和证据 ID | `agent_evidence` |
| 已完成动作 | 工具名、脱敏参数摘要、状态、调用签名 | 检查点 + `agent_tool_calls` |
| 完整工具结果 | 只放摘要、哈希和证据 ID | `agent_evidence` |
| 领域运行状态 | 去除内部字段并限深、限长的 JSON | `agent_checkpoints` |
| 下一步动作 | 基于最近工具结果确定的短指令 | `agent_checkpoints` |

安全输入容量按 `context_window - default_output_tokens - safety_margin_tokens` 计算。默认策略为：

1. 达到 55%：压缩较早的 assistant/tool 文本，不动身份、任务目标和最近轮次。
2. 达到 70%：保存检查点，自动将上下文重建为原 System 身份和一条结构化 User 检查点。
3. 重建目标为 35%；状态仍过大时继续收缩检查点，不删除证据原文。
4. 重建后达到 80%、累计重建 3 次或总 Token 预算耗尽时熔断。

重建属于同一个运行，累计 Token、轮次、工具次数和角色预算全部延续。成功的幂等副作用工具按
`tool_name + canonical_arguments` 签名去重，避免模型在恢复后重复提交。模型确需原始内容时，通过只读
`read_task_evidence(evidence_id, offset, max_chars)` 分页读取；工具只能访问当前运行的证据。

因此首次超过 80% 不需要人工清理上下文：Runtime 会先自动检查点化和重建。只有重建后仍超过熔断线、
任务自身的身份/工具定义过大、总预算不足或连续恢复次数耗尽时，才需要人工缩小任务范围或调整模型配置。

---

## 六、提取时机与闭环

### 6.1 触发点

| 触发时机 | 触发方式 | 提取内容 | 负责模型 |
|----------|----------|----------|----------|
| 用例生成完成后 | 流程内同步 | case_lessons（AI 识别到的业务规则、断言模式） | B |
| 报告分析完成后 | 流程内同步 | report_lessons（失败模式归类）、known_issues 更新 | C |
| 人工确认/编辑用例后 | Web 页面触发 | case_lessons（人工修正的预期/断言经验） | — |
| 接口文档变更后（maintain） | 流程内同步 | 相关 endpoint lessons 标记 review_status=needs_review | A |

### 6.2 Prompt 结构化输出

报告分析 prompt 末尾：

```
## 上下文提取要求

分析完成后，在响应末尾以 ```iatest-lessons-json 代码块输出本次提取的经验。

每条经验包含：scope, method, path, lesson_type, title, content, confidence, tags, environment
已有已知问题的状态更新以 ```iatest-known-issues-json 输出。
```

### 6.3 失败处理

提取失败写入 `extraction_jobs` 表：

```json
{
  "id": "ext-20260617-a1b2c3",
  "trigger": "report_analysis",
  "source": "report-20260617-101200.md",
  "status": "failed",
  "error": "LLM 响应中没有找到 iatest-lessons-json 代码块",
  "max_retries": 3,
  "retry_count": 1,
  "retry_backoff": "exponential",
  "next_retry_at": "2026-06-17T12:00:00"
}
```

后台线程定期扫描 `extraction_jobs` 表，重试到期的 `failed` 任务。超过 `max_retries` 标记 `permanent_failed`。

**提取失败不影响主流程**——用例生成/执行/报告分析本身不受影响。

---

## 七、已知问题生命周期

### 7.1 状态迁移

```
pending ──人工确认──→ active ──不再复现──→ resolved_candidate
  │                      │                    │
  │                      │ 再次复现（自动）     │ 人工确认
  │                      ↓                    ↓
  │                   reopened ──────────→ resolved ──60天→ archived
  │                      │                    │
  │                      │                    │ expires_at 到期
  │                      │                    ↓
  │                      │                 archived
  │                      │
  └──人工忽略──→ ignored
```

### 7.2 自动规则

| 规则 | 触发条件 | 行为 |
|------|----------|------|
| 复现检测 | 报告中同接口同类失败再次出现 | lifecycle: active → reopened |
| 候选解决 | 连续 5 次执行相关用例全部通过 + last_seen > 14 天 | 标记 resolved_candidate，Web 提醒 |
| 到期归档 | `expires_at` 到期 | lifecycle → archived |
| 长期静默 | resolved + last_seen > 60 天 | lifecycle → archived |
| 接口变更 | maintain 检测到接口变更 | 相关 lessons review_status → needs_review |

---

## 八、上下文注入审计 (ContextPack)

### 8.1 每次 AI 调用前保存

```json
{
  "id": "pack-20260617-a1b2c3",
  "task_id": "case-generation-sample-api-20260617",
  "task_type": "case_generation",
  "project": "sample-api",
  "model": "kimi-k2.6",
  "model_context_window": 128000,
  "context_budget": 12000,
  "reserve_output_tokens": 8000,
  "used_tokens": 7340,
  "included_lesson_ids": ["les-001", "les-002", "les-005"],
  "excluded_lesson_ids": [
    {"id": "les-003", "reason": "over_budget"},
    {"id": "les-004", "reason": "lifecycle_status_pending"},
    {"id": "les-006", "reason": "confidence_low"}
  ],
  "final_prompt_hash": "sha256:a1b2c3...",
  "created_at": "2026-06-17T10:30:00"
}
```

### 8.2 Web 可查询

用例详情弹窗中增加「AI 生成上下文」区域：

```
本次生成参考了以下经验：
✓ les-001: 邮箱不存在时仍返回 200（业务规则，已确认）
✓ les-002: prefix 参数可选（断言模式，已确认）
✓ les-005: 历史断言问题——不应预期 404（已确认）

以下经验因故未使用：
✗ les-003: token 预算不足
✗ les-004: 待确认
```

---

## 九、数据依赖管理

### 9.1 分层验证

```
规则引擎推断 ──→ verification_status = candidate
AI 推理      ──→ verification_status = candidate
    │
    └── 执行链路验证通过 ──→ verification_status = verified
    │                        lifecycle_status = active
    │
    └── 接口变更 ──→ verification_status = invalidated
                    review_status = needs_review
```

### 9.2 安全沙箱

依赖验证时的安全策略配置：

```yaml
context:
  dependency_verification:
    allow_methods: ["GET", "POST"]
    deny_methods: ["DELETE"]
    allowed_paths: ["/api/generate", "/api/check/*", "/api/login"]
    blocked_paths: ["/api/payment/*", "/api/order/delete/*"]
    requires_confirmation: true
    max_verification_attempts: 3
```

验证结果：

| 结果 | 含义 |
|------|------|
| `verified_safe` | 无副作用，验证通过 |
| `verified_with_side_effect` | 产生了数据变更但验证通过 |
| `verification_failed` | 验证执行失败 |
| `verification_skipped_by_policy` | 安全策略阻止 |

### 9.3 污染防护规则

| 规则 | 行为 |
|------|------|
| lifecycle_status=pending | 不注入 |
| confidence=low | 不注入 |
| review_status=rejected | 不注入 |
| lifecycle_status=archived / merged | 不注入 |
| 存在未解决 conflict | 不注入 |
| 接口变更后 needs_review | 不注入，先复核 |
| 同接口同类型的 lesson 被 reject 3 次以上 | 自动降低同类 AI 提取的 confidence |

**suspect 追踪（Phase 6）**：如果一条 lesson 被注入后，生成的用例在首次执行中失败 → 标记该 lesson 的 `quality_score` 降低，累积 3 次则 review_status → needs_review。

---

## 十、趋势分析

趋势分析的目标不是只统计"本次通过了多少"，而是利用上下文系统积累的结构化数据，发现跨多次执行的模式变化，为上下文注入和人工决策提供数据支撑。

### 10.1 数据来源

趋势分析的数据来自两个维度：

| 数据源 | 内容 | 采集时机 |
|--------|------|----------|
| `execution_trends` 表 | 每次执行后聚合的快照指标 | 报告生成后写入 |
| `failure_patterns` 表 | 跨多次执行汇总的失败模式 | 报告分析后增量更新 |
| `lessons` 表 | 已知问题的状态变化历史 | 实时 |

### 10.2 分析维度

#### 维度 1：接口级趋势

每个接口的指标变化曲线：

| 指标 | 计算方式 |
|------|----------|
| 通过率变化 | 近 10 次执行的通过率序列 |
| 响应时间趋势 | P50/P90/P95 的时间序列 |
| 失败类型分布 | 该接口每次失败归因类型占比变化 |
| 新增失败 | 上次通过但本次失败的用例 |
| 修复确认 | 上次失败但本次通过的用例（可能已修复） |

#### 维度 2：失败类型趋势

按失败归因类型聚合的宏观趋势：

| 归因类型 | 关注指标 |
|----------|----------|
| 接口缺陷 (interface_bug) | 占比是否在上升？新增还是复发？ |
| 用例预期错误 (case_assertion_error) | 占比下降意味着上下文系统在生效 |
| 测试数据问题 (test_data_issue) | 是否集中在特定接口？ |
| 环境问题 (env_issue) | 是否与特定时间段或环境相关？ |
| 已知问题复现 (known_issue_recurrence) | 哪些已知问题仍在活跃？哪些已沉寂？ |
| 新问题 (new_issue) | 是否在增加（可能接口变更/新功能上线）？ |

#### 维度 3：已知问题收敛性

追踪每个 known issue 的生命周期：

```
已知问题 les-001: GET /api/check/{email} 无效邮箱返回 200
  第 1 次出现: 06-14 report-001
  第 5 次出现: 06-17 report-005  ← 活跃
  最近 5 次执行中复现: 3 次        ← 未收敛
  趋势: stable (持续出现，未增多也未减少)

已知问题 les-002: DELETE /api/delete/{email} 偶发 500
  第 1 次出现: 06-12 report-003
  最近 5 次执行中复现: 0 次        ← 可能已修复
  趋势: improving → 建议标记 resolved_candidate
```

#### 维度 4：上下文系统自身效果

衡量上下文系统是否在改善用例质量：

| 指标 | 含义 | 理想方向 |
|------|------|----------|
| 用例预期错误占比 | 断言不合理导致的失败 | ↓ 下降（上下文在帮助修正） |
| 上下文注入命中率 | ContextPack 中被引用的 lesson 占比 | → 稳定（不过度注入） |
| 上下文修正数 | 因上下文修正过的用例数量 | → 逐步增长后稳定 |
| 上下文相关失败 | 注入的 lesson 被 suspect 标记的次数 | ↓ 下降（经验质量在提高） |

### 10.3 趋势数据的生成

每次报告分析完成后触发趋势更新：

```
报告分析完成
  ↓
1. 写入 execution_trends（本次执行快照）
  ↓
2. 更新 failure_patterns（增量合并本次失败模式）
  - 新出现的失败模式 → 新增记录
  - 已有失败模式 → occurrence_count += 1, last_seen = now
  - 未再出现的失败模式 → 计算 trend_direction（stable/improving/worsening）
  ↓
3. 趋势数据注入下次报告分析
  - prompt 中增加"近期趋势"段落
  - 帮助模型 C 判断"这是新问题还是趋势恶化"
```

### 10.4 趋势注入报告分析

报告分析 prompt 中增加：

```
## 近期趋势（近 10 次执行）

### 接口趋势
| 接口 | 近10次通过率 | 趋势 | 主要失败类型 |
|------|-------------|------|-------------|
| GET /api/check/{email} | 60% → 80% ↗ | improving | 已知问题复现(↓) |
| DELETE /api/delete/{email} | 40% → 40% → | stable | 环境问题 |

### 失败类型分布变化
| 类型 | 上次 | 本次 | 趋势 |
|------|------|------|------|
| 接口缺陷 | 3 (30%) | 2 (20%) | improving |
| 用例预期错误 | 5 (50%) | 2 (20%) | improving（上下文干预生效？） |
| 新问题 | 0 | 1 | 需关注 |

### 已知问题收敛
- les-001 "无效邮箱返回200"：活跃（5次出现，最近3次复现），未收敛
- les-002 "DELETE 偶发500"：5次未复现 → 建议标记 resolved_candidate

分析要求：
- 如果某类失败占比持续下降 → 说明修复或上下文干预在生效
- 如果某类失败突然增多 → 关注是否与接口变更、环境变更有关
- 如果已知问题长期未复现 → 在 known_issues 更新中建议标记 resolved_candidate
```

### 10.5 failure_patterns 的 trend_direction 判定

```python
def compute_trend_direction(pattern: dict, recent_results: list) -> str:
    """
    根据最近 N 次执行中该失败模式的出现频率判断趋势。
    """
    recent_occurrences = count_occurrences_in_last_n_runs(pattern, n=10)

    if len(recent_occurrences) < 3:
        return "new"  # 数据不足

    first_half = sum(recent_occurrences[:5])
    second_half = sum(recent_occurrences[5:])

    if first_half == 0 and second_half == 0:
        return "resolved"  # 已消失
    if second_half == 0 and first_half > 0:
        return "improving"  # 正在消失
    if second_half > first_half * 1.5:
        return "worsening"  # 恶化
    if abs(second_half - first_half) <= 1:
        return "stable"
    return "fluctuating"
```

### 10.6 Web 控制台：趋势视图

在「项目上下文」页面增加趋势 Tab：

```
┌──────────────────────────────────────────────────────────────┐
│ 项目上下文 / 趋势分析                   [project: sample-api] │
├──────────────────────────────────────────────────────────────┤
│ [经验库] [趋势分析] [审计日志]                                │
│                                                              │
│ 时间范围: [最近7天 ▼]  接口: [全部 ▼]                        │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────────────────┐  ┌──────────────────────┐         │
│  │  通过率趋势           │  │  失败类型分布变化      │         │
│  │  (迷你折线图/sparkline)│  │  (堆叠柱状图示意)     │         │
│  │  60%→80%↗           │  │  接口缺陷 ██         │         │
│  └──────────────────────┘  │  用例错误 ██ (↓)     │         │
│                             │  已知复现 █ (↓)      │         │
│  ┌──────────────────────┐  └──────────────────────┘         │
│  │  已知问题收敛状态      │                                   │
│  │  les-001 ⚠️ 活跃(3/5) │  ┌──────────────────────┐         │
│  │  les-002 ✅ 5次未复现 │  │  上下文效果            │         │
│  │  les-003 🔴 恶化      │  │  用例错误占比: 50%→20% │         │
│  └──────────────────────┘  │  注入命中率: 73%       │         │
│                             └──────────────────────┘         │
├──────────────────────────────────────────────────────────────┤
│ 接口趋势详情                                                 │
│ ┌──────────────────────────────────────────────────────────┐ │
│ │ GET /api/check/{email}                                   │ │
│ │ 通过率: ████████░░ 80% (↗ improving)                     │ │
│ │ 近5次: ✅ ✅ ✅ ❌ ✅   主要失败: 已知问题复现             │ │
│ │ 响应时间 P50: 120ms → 105ms (↘)                          │ │
│ └──────────────────────────────────────────────────────────┘ │
│ ┌──────────────────────────────────────────────────────────┐ │
│ │ DELETE /api/delete/{email}                               │ │
│ │ 通过率: ████░░░░░░ 40% (→ stable)                        │ │
│ │ 近5次: ❌ ❌ ✅ ❌ ✅   主要失败: 环境问题(偶发500)        │ │
│ │ 响应时间 P50: 350ms → 420ms (↗ 需关注)                   │ │
│ └──────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
```

注意：趋势视图不使用图表库，用纯文本/CSS 实现迷你趋势指示（sparkline 风格），保持零 JS 依赖。

---

## 十一、Web 控制台设计

### 10.1 「项目上下文」页面

```
┌──────────────────────────────────────────────────────────────┐
│ 项目上下文 / 经验库                    [project: sample-api]  │
│                                                              │
│ 健康概览: 待确认 5 · 冲突 2 · 待复核 3 · 近7天新增 12        │
├──────────────────────────────────────────────────────────────┤
│ [全部(42)] [待确认(5)] [冲突(2)] [业务规则] [已知问题] [依赖] │
│                                                              │
│ 筛选 ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐   │
│      │ 接口 ▼   │ │ 类型 ▼   │ │ 状态 ▼   │ │ 环境 ▼   │   │
│      └──────────┘ └──────────┘ └──────────┘ └──────────┘   │
│      🔍 搜索 (支持 FTS5 全文)                                │
├──────────────────────────────────────────────────────────────┤
│                                                              │
│ ⚠️ GET /api/check/{email}                pending · high     │
│    业务规则 · test 环境 · 出现 5 次                          │
│    邮箱不存在时仍返回 200，不应预期 404                       │
│    来源: report-20260616.md  report-20260617.md              │
│    [✓ 确认] [✗ 忽略] [✎ 编辑]                                │
│                                                              │
│ 🔴 冲突 GET /api/delete/{email}             conflict · needs_review │
│    A: "删除不存在邮箱返回 200" (来源: report-0614.md)         │
│    B: "删除不存在邮箱返回 404" (来源: report-0617.md)         │
│    [保留 A] [保留 B] [都保留(环境差异)] [都忽略]              │
│                                                              │
│ ✓ GET /api/generate                         active · confirmed │
│    断言模式 · test 环境 · 出现 12 次                          │
│    prefix 参数可选，省略时仍正常返回                          │
│    [停用] [✎ 编辑] [标记已解决]                               │
│                                                              │
│ ⚡ POST /api/login → POST /api/order/create  active · verified │
│    数据依赖 · execution_verified                              │
│    [标记失效] [查看链路]                                       │
│                                                              │
│ 🔗 相似经验待合并 (les-010 vs les-045)          merge        │
│    [合并] [保留两条] [忽略]                                   │
│                                                              │
├──────────────────────────────────────────────────────────────┤
│ 第 1-5 条，共 42 条                          [上一页] [下一页]│
└──────────────────────────────────────────────────────────────┘
```

### 10.2 审计日志

配置页面增加「审计日志」Tab，展示关键操作：

```
2026-06-17 10:00  user:wzl  确认 lesson les-001（待确认→已确认）
2026-06-17 10:15  user:wzl  合并 les-010 到 les-045
2026-06-17 10:30  system    注入上下文 pack-xxx（7,340 tokens, 3 lessons）
2026-06-17 11:00  system    提取 2 条新 lesson（报告 report-0617.md）
2026-06-17 11:05  system    标记 les-042 为 resolved_candidate（5 次未复现）
```

---

## 十二、落地计划

### Phase 1：存储层（约 1.5 天）
- [ ] SQLite 建表 + migration 框架
- [ ] Lesson CRUD（含三态拆分字段）
- [ ] lesson_relations / lesson_sources / extraction_jobs / audit_logs 表
- [ ] 内存索引构建（启动时从 SQLite 加载）
- [ ] JSONL 导出/导入（`iatest context export/import`）

### Phase 2：报告分析自动提取（约 2 天）
- [ ] 修改 `result_analysis.md` prompt，增加结构化 lessons 输出
- [ ] 解析 `iatest-lessons-json` 代码块
- [ ] Lesson 入库 + 去重检查 + 冲突检测
- [ ] 已知问题自动更新（复现 / resolved_candidate）
- [ ] 提取失败写入 extraction_jobs + 后台重试

### Phase 3：用例生成上下文注入（约 1.5 天）
- [ ] LLM 配置增加 `context_window` / `default_output_tokens`
- [ ] 动态 Token 预算计算
- [ ] 检索 + 确定性排序链 + 污染防护过滤 + 截断
- [ ] ContextPack 保存（每次 AI 调用前）
- [ ] `case_generation_user.md` 增加上下文注入段
- [ ] 用例生成完成后提取 case lessons

### Phase 4：报告分析上下文注入（约 1 天）
- [ ] 报告分析前注入 known_issues
- [ ] 模型 C 区分「新问题」vs「已知问题复现」
- [ ] 报告中展示已知问题状态追溯

### Phase 5：Web 页面（约 2 天）
- [ ] 「项目上下文」页面（经验库 / 趋势分析 / 审计日志三个 Tab）
- [ ] Lesson 确认 / 忽略 / 编辑 / 合并操作
- [ ] 冲突裁决界面
- [ ] 用例详情中展示 ContextPack
- [ ] 健康概览面板

### Phase 6：趋势分析（约 1.5 天，可与 Phase 5 并行）
- [ ] execution_trends 写入（报告生成后自动）
- [ ] failure_patterns 增量更新逻辑 + trend_direction 计算
- [ ] 报告分析 prompt 增加趋势段落
- [ ] Web 趋势视图（接口趋势 / 失败类型分布 / 已知问题收敛 / 上下文效果）
- [ ] `iatest context trend` CLI 命令

### Phase 7：深度能力（约 2 天）
- [ ] 依赖验证安全沙箱策略
- [ ] 污染防护 suspect 追踪
- [ ] 环境维度匹配规则
- [ ] quality_score 自动计算

### Phase 8：成熟度（按需）
- [ ] 治理健康视图增强
- [ ] 上下文命中率 / 误导率统计
- [ ] 自动整理建议（stale 提醒）
- [ ] ContextPack 对比分析

---

## 十三、CLI 命令

```bash
# 上下文管理
iatest context export                            # 导出 lessons 为 JSONL
iatest context import <file>                     # 从 JSONL 导入
iatest context migrate                           # 执行待应用的 migration
iatest context validate                          # 校验完整性
iatest context rebuild-index                     # 从 JSONL 重建 SQLite
iatest context stats                             # 上下文统计概览

# 趋势分析
iatest context trend                             # 趋势概览（近 10 次执行）
iatest context trend --endpoint "/api/login"     # 指定接口趋势
iatest context trend --days 30                   # 指定时间范围
iatest context trend --category failure_type     # 按失败类型分布查看

# 注入审计
iatest context pack <pack-id>                    # 查看某次注入详情
iatest context packs --project sample-api --limit 20 # 列出最近注入记录
```

---

## 十四、关键设计决策

| 决策 | 选择 | 原因 |
|------|------|------|
| 项目隔离 | 每项目独立 `context.db` + `project` 列二次保险 | 与框架现有 `results.sqlite3` 模式一致，物理隔离最安全 |
| 跨项目数据交换 | JSONL 导出/导入 | 不走跨库查询，避免锁竞争和路径耦合 |
| 运行时存储 | SQLite 主存储 | 并发安全、事务、查询、FTS5、迁移便利 |
| 便携/备份格式 | JSONL | 追加写入快、可流式处理、grep 友好 |
| 状态模型 | 三维拆分 (lifecycle + review + verification) | 单一 status 承载太多语义，拆分后正交清晰 |
| 去重方案 | 双层签名 + 冲突检测 | 确定性强去重 + 相似候选 + 矛盾不进合并 |
| Token 控制 | 模型感知动态计算 | 区别模型窗口大小，保证输出空间 |
| 排序策略 | 确定性优先级链 + quality_score 辅助 | 可解释可调试，质量分不篡权 |
| AI 输出 | 默认 lifecycle_status=pending | 不可直接污染长期记忆 |
| 依赖验证 | 全部 candidate 起点 | 规则引擎和 AI 都不完全可靠 |
| 污染防护 | 6 条过滤规则 + suspect 追踪 | 防止错误经验自循环 |
| 审计追溯 | ContextPack + audit_logs | 可回答"为什么生成这个用例" |
| 环境隔离 | environment 字段 + 匹配优先级 | 防止跨环境污染 |
| 趋势分析 | execution_trends + failure_patterns 双表 | 结构化快照支持趋势判定，不是简单统计 pass/fail |
| 趋势反馈 | 趋势数据注入报告分析 prompt | 帮助模型判断"新问题 vs 恶化 vs 收敛"，提升分析质量 |
