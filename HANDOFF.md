# 项目交接：全球趋势驱动新品机会系统

更新时间：2026-07-17
工作目录：`D:\code\xpzs`
当前分支：`main`
当前 HEAD：`b41111a Document current architecture, evidence gaps, and implementation roadmap`
Git 状态：`main` 与 `origin/main` 同步；本轮 README、HANDOFF 和两份研究文档更新尚未提交。不要擅自提交、重置或推送。

## 1. 产品目标与边界

系统目标不是把热点关键词直接转换成商品名，而是：

> 持续收集全球趋势信号，识别可能产生新实体消费品需求的变化，再用平台证据验证具体商品假设。

第一阶段只关注适合电商销售的低风险实体消费品，不包含软件、订阅、咨询、课程、资料包、危险品、医疗功效商品和未经授权的事件周边。

当前已经实现的下游证据链：

```text
TrendEvent
  -> OpportunitySignal
  -> ProductHypothesis
  -> MarketEvidence
  -> ValidatedRecommendation
```

事实、判断、假设和验证结果必须分层保存。缺少证据时允许为空或弃权，不允许用规则分、模型置信度、向量相似度或假设分冒充市场需求。

产品边界详见 `docs/product-boundary-and-architecture.md`。

目标研究链会在 `TrendEvent` 与 `OpportunitySignal` 之间加入：

```text
EvidenceBundle
  -> ResearchCandidate
  -> Research Agent + Skill
  -> OpportunityAssessment
  -> 人工确认
```

### 文档分工

- `README.md`：产品简介、当前能力和目标架构摘要。
- `docs/product-boundary-and-architecture.md`：长期有效的产品边界与五层业务对象约束。
- `docs/research-agent-architecture.md`：EvidenceBundle、工具、Skill、Agent、大模型和 MCP 的完整架构决策。
- `docs/research-agent-implementation-plan.md`：下一阶段开发的权威执行文档，包含模块、数据表、接口、测试、阶段和完成定义。
- `HANDOFF.md`：只维护当前完成状态、真实验证结果、绝对约束和下一轮入口。

如果文档出现冲突：产品边界以产品边界文档为准；研究组件设计以研究架构文档为准；具体开发顺序以实施计划为准；完成状态以本文件为准。

## 2. 当前实现状态

### 2.1 Phase 0–1：已完成

- 旧 `Analyzer/local-rules-v2` 活跃实现已删除；历史 `analyses` 只用于旧数据审计和 Signal 外键兼容。
- 未完成市场验证时，推荐分保持为空。
- 首页和飞书摘要只展示事实层趋势，不展示未验证商品榜单。
- 已建立独立 `opportunity_signals` 和反馈快照。
- 支持人工创建 OpportunitySignal，但必须引用本事件证据。
- 线索必须审核为 `follow_up` 才能创建商品假设。

### 2.2 Phase 2 工程闭环：已完成

- 可选模型：`intfloat/multilingual-e5-small`。
- 固定 revision：`614241f622f53c4eeff9890bdc4f31cfecc418b3`。
- 保存模型 ID、revision、输入哈希、特征版本、384 维向量和显式状态。
- 支持正负机会原型、实体类目原型和语义重复候选。
- 重复候选只进入人工复核，绝不自动合并。
- `/semantic-review` 支持真实事件标签、重复反馈和版本评测。

真实小样本结果：

- 7 条覆盖性人工标签。
- Embedding Precision@5：`0.20`。
- 趋势规则基线 Precision@5：`0.20`。
- 没有超过基线，因此 `ENABLE_EMBEDDINGS=false` 仍是默认值。
- 原 0.84 去重阈值产生 11 对误报，均已反馈为 `not_duplicate`。
- 默认重复候选阈值已收紧到 0.90。

本机模型资源：

- 缓存目录：`data/models`，约 470 MB，已被 Git 忽略。
- 首次联网准备约 170 秒。
- 纯本地冷启动约 48 秒。
- 工作集约 791 MB，私有提交约 2.64 GB。
- 第一条分析含加载约 40 秒，后续每条约 0.1–0.7 秒。

### 2.3 Phase 3：已完成独立主链路

- 独立 `product_hypotheses` 和反馈历史。
- `ProductHypothesisGenerator` Protocol。
- 首个稳定 Provider 为人工工作台。
- 必须引用已审核 OpportunitySignal 及其证据。
- 有实体商品、目标用户、场景、问题、差异、中心词和查询词结构。
- 非实体类型、IP、医疗功效、危险品等进入风险门。
- 阻断风险或缺少人工确认查询词时不能进入市场验证。
- 可选云端 Provider 只允许产生带引用的 OpportunityAssessment；Schema 不接受 ProductHypothesis 或推荐字段。

### 2.4 Phase 4：已完成独立主链路

- 独立 `market_evidence`。
- `MarketplaceDataProvider` Protocol。
- Seller Central Product Opportunity Explorer / Brand Analytics CSV Provider。
- 支持把平台证据与人工单位经济、执行证据组合成可审计的 composite evidence。
- 独立 `validated_recommendations`。
- 推荐必须同时满足：
  - 市场证据完整；
  - 单位经济评分至少 3；
  - 证据完整度评分至少 3；
  - 风险为 low 或 medium；
  - 能回溯完整对象链。

旧 `product_opportunities` 和旧验证 API 仍保留迁移兼容，但不会进入新推荐表。

### 2.5 Research 实施计划阶段 0–7：核心结构已完成，运行验收有缺口

- `evidence` 已增加内容强度、来源、抓取方法、标准化抓取状态、质量分、质量版本和原始元数据字段。
- 兼容迁移会把旧正文映射为 `full_article`、旧 hotlist 映射为 `title_only`，并把标题证据从 `valid_for_analysis` 中排除。
- 已新增不可变 `evidence_bundles` 快照、输入哈希、幂等持久化、历史分析事件回填和安全清理顺序。
- Pipeline 会为被分析事件持久化 EvidenceBundle；近期分析直接复用时也会补 Bundle。
- 已新增 `EventResearchView`，事件页按“结论、停止原因、类目联想、正负差值、证据覆盖、失败原因、缺失证据、下一步”展示。
- Embedding 的 disabled、unavailable、ready 和未运行状态以及人工标签均显示中文解释。
- “沈阳暴雨”式三条纯标题证据固定为 `insufficient`，即使标题来自两个不同域名也不会升级为 partial。
- 阶段 3：安全重定向逐跳校验公网地址，支持 meta、JSON-LD 和正文抽取、标准失败状态、Google Trends 关联新闻与人工证据 API。
- 阶段 4：新增 `research_candidates`、安全门、版本/Bundle 替换规则、`/research` 队列和完整 Candidate API；定时 Pipeline 在 Candidate 处结束。
- 阶段 5：新增可恢复 `research_runs`、不可变 `opportunity_assessments`、人工/规则 Provider、引用校验和人工审核；只有批准 `worth_following` 才创建 OpportunitySignal。
- 阶段 6：建立 `skills/trend-opportunity-research`，提供受控上下文、公开页面、关联新闻和 Bundle 重建工具；工具调用只保存请求哈希、状态、耗时和证据 ID，并执行租约、幂等、预算和凭证脱敏。
- 阶段 7：新增可选 OpenAI Structured Outputs Provider；低质量 Bundle 和敏感事件在模型前拦截，模型失败显式弃权，Schema 禁止 ProductHypothesis 字段。
- 事件页现在显示结论、停止原因、下一步、Candidate、Run 预算/工具审计、Assessment 和人工审核；研究工具补证据后会把当前 Candidate 前移到新的不可变 Bundle 快照。
- MCP 和浏览器登录态仍不是核心依赖，浏览器证据默认关闭。系统不会自动创建 OpportunitySignal、商品假设或推荐。

已知未完成验收：

- `candidate_from_event()` 要求语义特征状态为 `ready` 且存在类目联想；默认 `ENABLE_EMBEDDINGS=false` 时只会保存 `disabled` 特征，因此 Pipeline 不创建 ResearchCandidate。
- 真实库当前有 42 个 EvidenceBundle、7 个 ready 语义特征和 7 个 disabled 语义特征，但 ResearchCandidate、ResearchRun、OpportunityAssessment 均为 0。
- 这意味着表、API 和测试夹具中的人工研究闭环已经存在，但默认真实运行链还没有从 Bundle 自动进入 Candidate，不应宣称无模型闭环已完全贯通。
- 42 个真实 Bundle 中 27 个为 `insufficient`、15 个为 `partial`，没有 `ready_for_assessment`；正文证据覆盖仍是实际瓶颈。

## 3. 真实数据库状态

当前 `data/trends.db`：

```text
trend_events                    1156
evidence                          81
opportunity_signals                0
semantic_event_features           14
semantic_evaluation_labels         7
semantic_duplicate_candidates     11
product_hypotheses                 0
market_evidence                    0
validated_recommendations          0
```

零 OpportunitySignal、零商品假设和零推荐目前是合法结果，不代表数据库或页面故障。主要原因是：

- 定时 Pipeline 按设计只创建 ResearchCandidate，不自动创建 OpportunitySignal；
- 默认未配置 OpenAI OpportunityAssessment Provider；
- embedding 只用于候选特征，不允许自动生产线索；
- 真实证据覆盖不足。

## 4. 问题诊断与已实现解法

### 4.1 正文与证据获取不足

`app/evidence.py` 和 Evidence Collector 现在：

- 不执行 JavaScript；
- 不使用登录态或 Cookie；
- 手工跟随有限次重定向，并对每一跳重新执行公网/SSRF 校验；
- 抽取 meta、Open Graph、JSON-LD 和普通正文；
- 登录墙、访问拒绝、动态页面、非 HTML、超时和短正文都有标准失败状态；
- 只有标题的 hotlist 固定为 `title_only` 且不进入分析；
- Google Trends 关联新闻和人工 URL/正文/消费者评论可以进入 Bundle。

系统仍不会绕过验证码、登录或付费墙；浏览器证据默认关闭。不能依赖大模型弥补缺失正文，补不到公开证据时必须显式弃权。

已实现的 `EvidenceBundle` 包含：

```text
EvidenceBundle
  - full_text_count
  - title_only_count
  - independent_source_count
  - consumer_voice_count
  - fetch_failure_reasons
  - evidence_readiness
  - missing_evidence
```

证据获取按以下顺序工作：

1. 数据源原始摘要、正文或关联新闻 URL。
2. 来源专用公开解析器。
3. 同事件的无需登录独立新闻报道。
4. 公开社区消费者讨论。
5. 人工添加 URL、粘贴正文或评论。

不要把绕过登录、验证码或付费墙作为核心能力。

### 4.2 页面缺少可读的决策解释

事件页已使用确定性的 `EventResearchView`，不再把模型调试串作为主要解释。当前展示：

- 当前结论：形成线索 / 研究候选 / 证据不足 / 主动弃权。
- 类目候选及每项相似度。
- 正向、负向和机会差值。
- 差值的中文解释。
- 正文、标题、消费者声音和独立来源数量。
- 每项抓取失败原因。
- 当前生产器状态。
- 人工标签的中文解释。
- 为什么停在当前阶段。
- 重新判断所需的下一批证据。

### 4.3 “沈阳暴雨”案例

事件 #338 当前实际状态：

```text
类目相似度：
  出行户外 0.7927
  个护整理 0.7824
  汽车配件 0.7725

正向新品机会原型相似度：0.7487
负向/短时噪声原型相似度：0.7802
机会相似度差值：-0.0316

证据：3 条，全部只有热榜标题
抓取状态：content too short
人工标签：too_short_term
OpportunitySignal：0
```

旧 `local-rules-v1` 曾生成“场景化出行应急收纳包”和“动态出行清单助手”，现已 `superseded`。它们正是关键词模板容易产生的伪机会。

“雨衣”是成熟存量商品。只有出现可持续变化和具体未满足需求，例如极端降雨频率变化、传统雨衣无法覆盖背包、闷热、湿面收纳等证据时，才可能形成新品机会线索。

## 5. 程序、Skill 与 Agent 的正确边界

当前实现采用混合架构，不应改成“只做一个 Skill/Agent”，也不应把所有研究判断重新硬编码进定时程序。

### 5.1 核心程序必须保留

程序负责确定性、可审计和长期运行的部分：

- 数据采集和原始响应审计；
- 去重、聚类、事件状态和定时任务；
- 数据库与对象链；
- 证据引用、版本和哈希；
- 风险门、状态机和推荐资格；
- 市场证据解析与确定性评分；
- 权限、幂等、通知和历史快照；
- 页面、API 和训练/评测数据。

这些能力不适合只存在于 Agent 对话或 Skill 文本中。否则无法保证持续运行、可追溯、状态一致和推荐门槛。

### 5.2 Skill/Agent 适合承担

Agent/Skill 负责开放式研究和需要工具编排的部分：

- 为证据不足事件搜索公开替代来源；
- 汇总跨语言新闻和消费者讨论；
- 判断变化是否可能影响消费行为；
- 形成带引用、允许弃权的 OpportunityAssessment；
- 建议缺失证据和下一步研究计划；
- 在人工确认后创建或补全 OpportunitySignal；
- 从已审核线索生成 ProductHypothesis；
- 编排 Seller Central、成本表和其他授权平台研究。

Agent 只能通过受控 API 写回数据库，不能绕过状态和风险门，也不能成为唯一事实来源。

### 5.3 当前研究链路

```text
TrendEvent
  -> EvidenceBundle
  -> ResearchCandidate
  -> OpportunityAssessment
  -> OpportunitySignal
  -> ProductHypothesis
  -> MarketEvidence
  -> ValidatedRecommendation
```

- Embedding：去重、检索、跨语言匹配和类目联想。
- 大模型：基于 EvidenceBundle 做结构化研究判断和可读解释。
- Skill/Agent：编排搜索、工具和人工协作。
- 核心程序：保存事实、执行状态机、校验证据和决定资格。

大模型不能解决无法访问的正文。只有标题时，大模型的正确行为仍应是弃权或创建“待研究候选”，而不是凭常识推荐雨衣。

## 6. 后续运营优先级

研究架构的核心对象和接口已完成，但默认配置下的 Candidate 入口仍需补齐；在此之前不能只做样本运营并宣称程序主链已经完全验收。

### P0：补齐默认 Candidate 入口（尚未实施）

1. 设计 `ENABLE_EMBEDDINGS=false` 时的确定性类目/待研究候选策略，或允许无类目 Candidate 进入补证队列。
2. 保持 Candidate 不生成商品名、查询词或需求结论。
3. 用真实 Pipeline 验证 Bundle -> Candidate，而不是只依赖测试中的 ReadyModelExtractor。

### P1：真实研究样本与人工评测

1. 在 `/research` 从高优先级 Candidate 启动人工 ResearchRun。
2. 收集公开正文、独立来源和消费者声音，保留工具审计。
3. 形成带逐条引用的人工 OpportunityAssessment 样本。
4. 记录弃权、需要更多证据和批准结果，作为后续评测集。

### P2：云端 Provider 灰度评测

1. 只在 EvidenceBundle 已就绪且事件未命中安全门时调用。
2. 与人工 Assessment 对照引用完整性、弃权率和审核通过率。
3. 不改变人工审核、Signal 资格或下游推荐门槛。

### P3：可选执行基础设施

1. 只有出现明确跨进程需求时再增加 MCP server；核心程序不依赖 MCP。
2. 只有经授权且合规需求明确时评估浏览器证据；不得持久化 Cookie 或登录页。
3. 若增加独立 worker，继续复用 Candidate 租约、预算和受控工具接口。

## 7. 验收标准

- 用户能一眼看懂为什么某事件没有形成线索。
- 标题证据不会被显示成完整正文证据。
- 无正文时系统先补充公开来源，补不到则明确弃权。
- 类目相似度不会被误解为市场概率或新品结论。
- 大模型每个事实和判断都有可回溯引用。
- Skill/Agent 中断后，数据库事实和状态不丢失。
- 任何推荐仍能完整回溯五层对象链。

## 8. 验证状态

最后完整验证：

```text
.venv\Scripts\python.exe -m pytest -q
  -> 58 passed，1 个上游 TestClient 弃用警告

.venv\Scripts\python.exe -m pytest -q tests\test_core.py
  -> 56 passed，1 个上游 TestClient 弃用警告

python -m compileall -q app
  -> passed

python -m ruff check app tests
  -> passed

git diff --check
  -> passed，仅 Windows LF/CRLF 提示

PRAGMA foreign_key_check
  -> []

Skill quick_validate
  -> Skill is valid!
```

真实 `data/trends.db` 未在本轮验证中改写，验证前后 SHA-256 一致。数据库副本执行初始化迁移后生成 42 个历史 EvidenceBundle，42 个已分析事件缺失 Bundle 数为 0；事件 #338 的 Bundle 为 `insufficient`、质量分 0.30、完整正文 0、标题证据 3、独立来源 2，页面结构化解释验证通过。

全量测试包含真实外部来源。单次 NewsNow/Google Trends 失败应先单独复测，不要直接判定代码回归。

## 9. 重要文件

- 产品边界：`docs/product-boundary-and-architecture.md`
- 目标研究架构：`docs/research-agent-architecture.md`
- 可执行实施计划：`docs/research-agent-implementation-plan.md`
- 正文抓取：`app/evidence.py`
- Evidence 类型与 Bundle：`app/evidence_types.py`、`app/evidence_bundle.py`
- Evidence Collector：`app/evidence_collectors.py`
- ResearchCandidate：`app/research_candidates.py`
- ResearchRun 与受控工具：`app/research.py`、`app/research_tools.py`
- OpportunityAssessment：`app/opportunity_assessment.py`
- Research Skill：`skills/trend-opportunity-research/`
- 数据库：`app/db.py`
- 人工 Signal 输入 Schema：`app/opportunity_signals.py`
- 语义特征：`app/semantic.py`
- 语义重复：`app/semantic_duplicates.py`
- 商品假设：`app/product_hypotheses.py`
- 市场证据 Provider：`app/market_evidence.py`
- 管道：`app/pipeline.py`
- 页面和 API：`app/main.py`
- 事件页：`app/templates/event.html`
- 语义评测页：`app/templates/semantic_review.html`
- 回归测试：`tests/test_core.py`

## 10. 绝对约束

1. 不恢复新闻关键词到固定商品模板。
2. 不把类目相似度或模型置信度称为需求概率。
3. 不用大模型补写无法抓取的事实。
4. 不强行填满机会或推荐榜。
5. 不自动合并语义相似事件。
6. 不允许 Skill/Agent 绕过数据库状态和证据门槛。
7. 不把 Agent 对话当成唯一事实来源。
8. 不在默认测试或启动时联网下载模型。
9. 不删除反馈、评测和推荐快照。
10. 不执行破坏性数据库重建而不先备份。
11. 不擅自提交、重置、推送或清理工作区。
12. Windows 环境不要使用 `rg`，使用 PowerShell 原生命令。

## 11. 新会话建议先执行

```powershell
Set-Location -LiteralPath 'D:\code\xpzs'
git status --short --branch
git log -3 --oneline
Get-Content -LiteralPath 'HANDOFF.md' -Raw
python -m compileall -q app
python -m pytest -q tests\test_core.py
```

最合理的续做入口是：

> 启动本地应用，从 `/research` 选择真实 Candidate，按 Skill 流程完成人工补证据和 OpportunityAssessment，积累可审核样本；在样本质量足够前保持 `ENABLE_RESEARCH_AGENT=false`，不要自动生产 Signal。
