# EvidenceBundle 与 Research Agent 可执行实施计划

> 实现状态（2026-07-17）：阶段 0–7 的核心对象、表、接口和安全边界已实现，但并非全部通过运行验收。默认 `ENABLE_EMBEDDINGS=false` 时 Pipeline 无法创建 ResearchCandidate，因而不满足“各阶段可在 Embedding 关闭下运行”的原则；真实数据库的证据 Bundle 也尚无 `ready_for_assessment`。本文继续作为设计与验收契约，真实验证结果与后续入口以 `HANDOFF.md` 为准。

状态：待实施
日期：2026-07-17
依赖架构：`docs/research-agent-architecture.md`
目标：把当前“标题证据 + 原始语义指标”升级为“可审计证据包 + 待研究候选 + 可选 Agent 研究 + 结构化机会评估”。

## 1. 实施原则

1. 先完成无大模型闭环，再接入 Agent 和大模型。
2. 每个阶段必须独立可测试、可回滚、可在 `ENABLE_EMBEDDINGS=false` 下运行。
3. 新对象使用独立表，不把研究字段继续塞入旧 `product_opportunities`。
4. EvidenceBundle 和 OpportunityAssessment 使用不可变快照；新版本不覆盖旧版本。
5. 所有生成对象必须引用数据库中存在的证据 ID。
6. ResearchCandidate、OpportunityAssessment 和 OpportunitySignal 是三个不同对象。
7. 默认服务启动、测试和普通采集不得联网下载模型。
8. 不在本实施中创建或推送任何没有市场证据的推荐。

## 2. 改造前代码基线

现有相关模块：

- `app/evidence.py`：通用 HTTP 抓取和 HTML 段落抽取。
- `app/sources.py`：NewsNow、Google Trends、Reddit 等来源。
- 旧 `app/pipeline.py::_research_and_analyze`：插入 hotlist 证据、抓取最多 3 个 URL、保存语义特征并调用 Analyzer。
- `app/semantic.py`：Embedding、正负原型和类目检索。
- 旧 `app/analysis.py`：OpportunitySignal Schema、可选 OpenAI 分析和 `local-rules-v2` fallback；活跃实现现已删除。
- `app/db.py`：SQLite Schema 和兼容迁移。
- `app/main.py`：页面、API 和审核流程。
- `app/templates/event.html`：事件详情。
- `tests/test_core.py`：核心回归测试。

当前限制：

- `fetch_evidence` 不跟随重定向、不执行 JavaScript、无来源专用策略。
- 抓取失败时正文回退成标题。
- hotlist 标题和完整正文没有稳定的内容强度类型。
- 没有 EvidenceBundle、ResearchCandidate、ResearchRun 或 OpportunityAssessment 表。
- 事件页直接展示模型调试字段。

## 3. 目标模块结构

新增模块：

```text
app/evidence_types.py
app/evidence_bundle.py
app/evidence_collectors.py
app/research_candidates.py
app/research.py
app/opportunity_assessment.py
app/templates/research_queue.html
```

后续 Skill：

```text
skills/trend-opportunity-research/
  SKILL.md
  references/evidence-quality.md
  references/source-routing.md
  references/abstention-rules.md
  schemas/opportunity-assessment.json
```

注意：Skill 目录只有在核心 API 和无大模型闭环完成后才创建。

## 4. 数据库变更

### 4.1 扩展 `evidence`

通过 `Database._ensure_column` 增加：

```sql
evidence_type TEXT NOT NULL DEFAULT 'title_only'
source_name TEXT NOT NULL DEFAULT ''
fetch_method TEXT NOT NULL DEFAULT 'unknown'
fetch_status TEXT NOT NULL DEFAULT 'unknown'
quality_score REAL NOT NULL DEFAULT 0
quality_version TEXT NOT NULL DEFAULT 'evidence-quality-v1'
raw_metadata_json TEXT NOT NULL DEFAULT '{}'
```

允许的 `evidence_type`：

```text
full_article
official_notice
article_summary
consumer_discussion
consumer_comment
search_snippet
title_only
manual_evidence
```

允许的 `fetch_status`：

```text
ready
content_too_short
login_required
redirect_blocked
javascript_required
not_html
http_error
timeout
robots_or_access_denied
unsupported
failed
```

迁移规则：

- `kind='article'` 且正文有效：`full_article`。
- `kind='hotlist'`：`title_only`。
- 有 `error` 的旧记录根据错误文本映射 `fetch_status`，无法映射则 `failed`。

### 4.2 新增 `evidence_bundles`

```sql
CREATE TABLE evidence_bundles (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NOT NULL REFERENCES trend_events(id),
  input_hash TEXT NOT NULL,
  version TEXT NOT NULL,
  readiness_status TEXT NOT NULL,
  readiness_score REAL NOT NULL,
  full_text_count INTEGER NOT NULL,
  title_only_count INTEGER NOT NULL,
  independent_source_count INTEGER NOT NULL,
  consumer_voice_count INTEGER NOT NULL,
  official_source_count INTEGER NOT NULL,
  evidence_ids_json TEXT NOT NULL DEFAULT '[]',
  fetch_failure_reasons_json TEXT NOT NULL DEFAULT '[]',
  missing_evidence_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  UNIQUE(event_id, input_hash, version)
);
```

索引：

```sql
CREATE INDEX idx_evidence_bundles_event
ON evidence_bundles(event_id, id DESC);
```

### 4.3 新增 `research_candidates`

```sql
CREATE TABLE research_candidates (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NOT NULL REFERENCES trend_events(id),
  evidence_bundle_id INTEGER NOT NULL REFERENCES evidence_bundles(id),
  semantic_feature_id INTEGER REFERENCES semantic_event_features(id),
  candidate_reason TEXT NOT NULL,
  category_candidates_json TEXT NOT NULL DEFAULT '[]',
  positive_similarity REAL,
  negative_similarity REAL,
  opportunity_delta REAL,
  research_questions_json TEXT NOT NULL DEFAULT '[]',
  missing_evidence_json TEXT NOT NULL DEFAULT '[]',
  priority REAL NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending',
  engine TEXT NOT NULL,
  version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

状态：

```text
pending
researching
evidence_ready
insufficient_evidence
awaiting_review
completed
failed
superseded
```

### 4.4 新增 `research_runs`

```sql
CREATE TABLE research_runs (
  id TEXT PRIMARY KEY,
  candidate_id INTEGER NOT NULL REFERENCES research_candidates(id),
  executor_type TEXT NOT NULL,
  executor_name TEXT NOT NULL,
  status TEXT NOT NULL,
  budget_json TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  error TEXT
);
```

`executor_type`：`human`、`rules`、`agent`。

### 4.5 新增 `research_tool_calls`

```sql
CREATE TABLE research_tool_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES research_runs(id),
  tool_name TEXT NOT NULL,
  request_hash TEXT NOT NULL,
  status TEXT NOT NULL,
  result_evidence_ids_json TEXT NOT NULL DEFAULT '[]',
  latency_ms INTEGER NOT NULL,
  error TEXT,
  created_at TEXT NOT NULL
);
```

不得保存 Cookie、令牌、完整登录页面或搜索 API 密钥。

### 4.6 新增 `opportunity_assessments`

```sql
CREATE TABLE opportunity_assessments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  candidate_id INTEGER NOT NULL REFERENCES research_candidates(id),
  evidence_bundle_id INTEGER NOT NULL REFERENCES evidence_bundles(id),
  research_run_id TEXT REFERENCES research_runs(id),
  assessment_status TEXT NOT NULL,
  change_type TEXT NOT NULL DEFAULT '',
  consumer_relevance TEXT NOT NULL DEFAULT '',
  durability TEXT NOT NULL DEFAULT '',
  lead_time_fit TEXT NOT NULL DEFAULT '',
  target_users_json TEXT NOT NULL DEFAULT '[]',
  new_scenarios_json TEXT NOT NULL DEFAULT '[]',
  unmet_needs_json TEXT NOT NULL DEFAULT '[]',
  related_product_categories_json TEXT NOT NULL DEFAULT '[]',
  fact_claims_json TEXT NOT NULL DEFAULT '[]',
  inferences_json TEXT NOT NULL DEFAULT '[]',
  evidence_ids_json TEXT NOT NULL DEFAULT '[]',
  missing_evidence_json TEXT NOT NULL DEFAULT '[]',
  abstention_reason TEXT NOT NULL DEFAULT '',
  review_status TEXT NOT NULL DEFAULT 'pending',
  engine TEXT NOT NULL,
  model TEXT NOT NULL DEFAULT '',
  version TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

`assessment_status`：

```text
worth_following
abstained
insufficient_evidence
```

`review_status`：

```text
pending
approved
rejected
needs_more_evidence
superseded
```

### 4.7 清理顺序

更新 `Database.clear_derived_data` 时，外键删除顺序必须为：

```text
opportunity_assessments
research_tool_calls
research_runs
research_candidates
evidence_bundles
semantic_event_features
semantic_evaluation_labels
analyses
evidence
...
```

执行后必须通过 `PRAGMA foreign_key_check`。

## 5. Python Schema 与 Protocol

### 5.1 `app/evidence_types.py`

定义：

```python
class EvidenceType(StrEnum): ...
class FetchStatus(StrEnum): ...

class ManualEvidenceInput(BaseModel):
    evidence_type: EvidenceType
    source_name: str
    url: str = ""
    title: str
    excerpt: str
    is_consumer_voice: bool = False
    note: str = ""
```

### 5.2 `app/evidence_collectors.py`

```python
class EvidenceCollector(Protocol):
    name: str

    async def collect(
        self,
        event: dict,
        current_evidence: list[dict],
        budget: ResearchBudget,
    ) -> list[CollectedEvidence]: ...
```

第一版实现：

- `DirectPublicPageCollector`。
- `RelatedNewsCollector`，先只消费 SourceItem 原始 JSON 中已有的关联 URL。
- `ManualEvidenceCollector`。

第二版再实现搜索 Provider 和浏览器 Provider。

### 5.3 `app/evidence_bundle.py`

纯函数优先：

```python
def classify_evidence_strength(row: dict) -> EvidenceType: ...

def calculate_evidence_quality(row: dict) -> float: ...

def build_evidence_bundle(
    event: dict,
    evidence: list[dict],
    version: str = "evidence-bundle-v1",
) -> EvidenceBundleResult: ...
```

第一版权重建议：

```text
official_notice        1.00
full_article           0.90
consumer_discussion    0.85
consumer_comment       0.75
article_summary        0.55
search_snippet         0.30
title_only             0.10
```

准备度必须同时考虑内容强度和来源多样性，不能只累加数量。

第一版 `ready_for_assessment` 最低条件：

- 至少 2 个独立来源；并且
- 至少 1 条完整正文或官方公告；并且
- 总质量分达到配置阈值；并且
- 不全部来自同一个热榜聚合器。

消费者声音不是所有事件的强制条件，但缺失时必须进入 `missing_evidence`。

### 5.4 `app/research_candidates.py`

```python
def candidate_from_event(
    event: dict,
    bundle: EvidenceBundleResult,
    semantic_feature: dict | None,
) -> ResearchCandidateDraft | None: ...
```

规则：

- EvidenceBundle 不足时允许创建 ResearchCandidate。
- ResearchCandidate 只能包含类目联想和研究问题。
- 不生成商品名、Amazon 查询词、目标售价或需求结论。
- 安全门命中的悲剧、犯罪和高风险事件默认不创建商业研究候选；可保留事实跟踪状态。

### 5.5 `app/opportunity_assessment.py`

```python
class CitedClaim(BaseModel):
    claim: str
    evidence_ids: list[int]

class OpportunityAssessmentDraft(BaseModel):
    assessment_status: Literal[
        "worth_following", "abstained", "insufficient_evidence"
    ]
    change_type: str = ""
    consumer_relevance: str = ""
    durability: str = ""
    lead_time_fit: str = ""
    target_users: list[str] = []
    new_scenarios: list[str] = []
    unmet_needs: list[str] = []
    related_product_categories: list[str] = []
    fact_claims: list[CitedClaim] = []
    inferences: list[CitedClaim] = []
    evidence_ids: list[int] = []
    missing_evidence: list[str] = []
    abstention_reason: str = ""
```

```python
class OpportunityAssessmentProvider(Protocol):
    async def assess(
        self,
        event: dict,
        bundle: dict,
        candidate: dict,
        evidence: list[dict],
    ) -> OpportunityAssessmentResult: ...
```

第一版只实现：

- `HumanAssessmentProvider`。
- `AbstainingRulesAssessmentProvider`。

大模型 Provider 在阶段 7 接入。

## 6. 配置项

新增到 `Settings` 和 `.env.example`：

```text
EVIDENCE_BUNDLE_VERSION=evidence-bundle-v1
EVIDENCE_READY_SCORE=1.8
RESEARCH_CANDIDATE_VERSION=research-candidate-v1
RESEARCH_MAX_SEARCH_QUERIES=8
RESEARCH_MAX_FETCH_PAGES=15
RESEARCH_MAX_BROWSER_PAGES=3
RESEARCH_TIMEOUT_SECONDS=300
ENABLE_RESEARCH_AGENT=false
ENABLE_BROWSER_EVIDENCE=false
```

默认关闭 Research Agent 和浏览器证据。单元测试不得访问搜索服务或浏览器。

## 7. API 设计

### 7.1 Evidence

```text
GET  /api/events/{event_id}/evidence
POST /api/events/{event_id}/evidence/manual
POST /api/events/{event_id}/evidence-bundle/rebuild
GET  /api/events/{event_id}/evidence-bundles
```

手工证据写入要求：

- 本机或 Admin Token。
- URL 可空；有 URL 时执行公网地址检查。
- 保存人工来源、创建时间和内容哈希。
- 不覆盖已有证据。

### 7.2 Research Candidate

```text
GET  /api/research-candidates
GET  /api/research-candidates/{candidate_id}
POST /api/events/{event_id}/research-candidates
POST /api/research-candidates/{candidate_id}/status
```

### 7.3 Research Run

```text
POST /api/research-candidates/{candidate_id}/runs
GET  /api/research-runs/{run_id}
POST /api/research-runs/{run_id}/tool-results
POST /api/research-runs/{run_id}/complete
```

Agent 不获得任意 SQL 接口。

### 7.4 Opportunity Assessment

```text
GET  /api/opportunity-assessments
POST /api/research-candidates/{candidate_id}/assessments
POST /api/opportunity-assessments/{assessment_id}/review
```

批准 Assessment 时：

1. 再次校验 EvidenceBundle 和证据引用。
2. 将结构化字段映射到 OpportunitySignal。
3. Signal 的 `engine/model/version` 来自 Assessment。
4. 保存包含 Event、Bundle、Candidate、Assessment 和 Evidence 的审核快照。

## 8. 页面改造

### 8.1 事件详情

新增 `build_event_research_view`，不要在 Jinja 中堆判断逻辑。

ViewModel：

```python
class EventResearchView(BaseModel):
    conclusion_code: str
    conclusion_label: str
    stop_reasons: list[str]
    category_candidates: list[CategoryCandidateView]
    positive_similarity: float | None
    negative_similarity: float | None
    opportunity_delta: float | None
    delta_explanation: str
    evidence_summary: EvidenceSummaryView
    fetch_failures: list[FetchFailureView]
    missing_evidence: list[str]
    producer_status: str
    human_label: str
    next_actions: list[str]
```

展示顺序：

1. 当前结论。
2. 为什么停止。
3. 探索性类目及逐项相似度。
4. 正负相似度和差值解释。
5. 证据覆盖与抓取失败。
6. 缺失证据。
7. 启动研究或人工补证据操作。
8. Assessment 和审核后的 Signal。

不得再显示一整行 `ready · model@revision · ...` 作为主要解释。

### 8.2 研究队列

新增 `/research`：

- pending、researching、insufficient 和 awaiting_review 分组。
- 显示研究优先级、证据准备度和缺失证据。
- 支持人工启动 Research Run。
- 默认不自动启动 Agent。

## 9. 管道改造

改造前：

```text
抓取证据
-> 保存语义特征
-> Analyzer
-> OpportunitySignal
```

目标：

```text
抓取基础证据
-> EvidenceBundle
-> 保存语义特征
-> ResearchCandidate
-> 结束定时管道
```

定时管道默认不调用 Research Agent 或大模型。

Agent 由以下方式触发：

- 用户在研究队列手工启动。
- 后续独立 worker 对高优先级候选限额启动。
- API 明确调用。

旧 `Analyzer/local-rules-v2` 从活跃代码删除，不恢复商品模板；历史数据库记录保持只读兼容。

## 10. 实施阶段

### 阶段 0：特征化当前失败案例

编码前先新增回归测试：

- “沈阳暴雨”式 3 条 title-only 证据必须得到 `insufficient`。
- title-only 不得被计作 full text。
- 页面必须显示 `content_too_short` 中文原因。
- 类目相似度逐项展示。
- 机会差值为负时显示“负向判断略强”。

验收：测试先失败，证明覆盖真实问题。

### 阶段 1：Evidence 类型和 EvidenceBundle

改动：

- Schema 和兼容迁移。
- `app/evidence_types.py`。
- `app/evidence_bundle.py` 纯函数。
- Bundle 持久化和解码。
- Pipeline 为被分析事件创建 Bundle。

测试：

- Evidence 类型迁移。
- Bundle 输入哈希和幂等。
- 标题、正文、官方来源和消费者声音计数。
- 独立域名计数。
- 准备度阈值。
- `clear_derived_data` 外键顺序。

验收：不依赖网络、模型或大模型即可完整运行。

### 阶段 2：结构化事件页

改动：

- `build_event_research_view`。
- 事件页新的结论、原因、类目、证据和下一步区域。
- 状态和标签中文映射。

测试：

- title-only 页面解释。
- ready evidence 页面解释。
- embedding disabled/unavailable/ready 三种状态。
- 没有标签、没有语义特征和没有证据时不报错。

验收：用户不阅读模型术语也能理解为什么没有线索。

### 阶段 3：基础证据获取改进

改动：

- 安全重定向，每次跳转重新验证公网目标。
- meta、JSON-LD 和正文抽取。
- 标准化失败原因。
- Google Trends 关联新闻 URL Collector。
- 人工证据 API。

暂不实现：

- 登录微博自动化。
- 验证码处理。
- 任意浏览器 Agent。

测试使用本地 MockTransport，不访问真实站点。

验收：公开页面和关联新闻能进入 EvidenceBundle，登录墙明确显示失败而不伪装正文。

### 阶段 4：ResearchCandidate

改动：

- 数据表和 Schema。
- 候选生成纯函数。
- Pipeline 创建待研究候选。
- `/research` 页面和 API。

测试：

- 类目联想可以创建候选但不创建 Signal。
- 高风险事件不创建商业研究候选。
- 候选版本变化会 supersede 旧候选。
- 零候选合法。

验收：沈阳暴雨可以显示为“待研究防雨方向”，但 `/signals` 仍为空。

### 阶段 5：无大模型人工研究闭环

改动：

- ResearchRun 和工具审计表。
- HumanAssessmentProvider。
- Assessment API 和审核。
- 批准 Assessment 后创建 Signal。

测试：

- 证据不足无法批准为 Signal。
- 引用不存在或属于其他事件时拒绝。
- 人工补证据后可以重建 Bundle 并重新 Assessment。
- 审核快照包含完整对象链。

验收：没有大模型也能从 Candidate 完成人工研究到 Signal。

### 阶段 6：Research Skill 与工具接口

前置条件：阶段 1–5 全部通过。

改动：

- 建立 `trend-opportunity-research` Skill。
- 将 Evidence 和 Research API 暴露为 Agent 工具。
- 如果需要跨进程调用，再增加 MCP server；否则先用直接 API。
- 工具调用写入 `research_tool_calls`。

验收：Agent 中断后可以从 ResearchRun 状态恢复，重复调用不产生重复证据。

### 阶段 7：大模型 OpportunityAssessment

前置条件：已有高质量 EvidenceBundle 和人工 Assessment 样本。

改动：

- `CloudOpportunityAssessmentProvider`。
- Prompt 只接收结构化 Bundle 和证据。
- Schema 和引用校验。
- 低准备度强制在调用模型前弃权。
- 模型输出不能包含 ProductHypothesis。

测试：

- 未知证据 ID 被拒绝。
- title-only Bundle 不调用模型。
- 模型失败保持显式 failed/abstained。
- 敏感事件在模型前被安全门拦截。

验收：大模型提高人工研究效率，但不改变下游状态和资格规则。

## 11. 第一轮具体开发任务

本节记录已经完成的第一轮阶段 0–2 范围；当时未同时接入 Agent：

1. 为 Evidence 增加类型、抓取状态和质量字段。
2. 新增 `app/evidence_types.py`。
3. 新增 `app/evidence_bundle.py` 及纯函数测试。
4. 新增 `evidence_bundles` 表、解码和清理顺序。
5. Pipeline 持久化 Bundle。
6. 建立 `EventResearchView`。
7. 重构事件页语义和证据区域。
8. 使用事件 #338 或等价 fixture 验证结构化解释。
9. 运行核心测试、全量测试、Ruff、compileall、diff check 和外键检查。

第一轮明确不做：

- Search MCP。
- 浏览器登录态。
- Research Agent。
- 大模型 Assessment。
- 自动 OpportunitySignal。

## 12. 测试清单

单元测试：

- Evidence 类型映射。
- 抓取失败原因映射。
- Evidence 质量分。
- Bundle 输入哈希。
- Bundle 准备度。
- 类目差值中文解释。
- Candidate 安全门。
- Assessment 引用校验。

API 测试：

- 手工证据写入。
- Bundle 重建。
- Candidate 列表和状态。
- Research Run 幂等。
- Assessment 审核与 Signal 创建。

页面测试：

- 证据不足。
- 证据准备完成。
- embedding disabled、unavailable 和 ready。
- Agent 未配置和运行失败。

安全测试：

- 私网 URL 和重定向到私网被阻止。
- 未授权写 API 被拒绝。
- 工具日志不包含凭证。
- 超出研究预算停止执行。

回归命令：

```powershell
python -m compileall -q app
python -m ruff check app tests
python -m pytest -q tests\test_core.py
.venv\Scripts\python.exe -m pytest -q
git diff --check
```

数据库检查：

```python
db.all("PRAGMA foreign_key_check") == []
```

## 13. 完成定义

本实施完成必须同时满足：

- 程序能明确区分标题、摘要、正文、官方来源和消费者声音。
- 每个被分析事件都有 EvidenceBundle 快照。
- 证据不足不会调用大模型或创建 OpportunitySignal。
- ResearchCandidate 可以保留研究方向但不能进入商品验证。
- 无大模型模式可以完成人工研究闭环。
- Agent/Skill 只能通过受控工具和 API 工作。
- 用户能从事件页理解结论、停止原因和下一步。
- 所有事实、推断和状态均可回溯。
- 所有测试和外键检查通过。
