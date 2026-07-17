# 项目交接：全球趋势驱动新品机会系统

更新时间：2026-07-17
工作目录：`D:\code\xpzs`
当前分支：`main`
当前 HEAD：`a8616bb Refactor application structure and improve functionality`
Git 状态：`main` 比 `origin/main` 超前 1 个提交；更新本文前工作区干净。不要擅自提交、重置或推送。

## 1. 产品目标与边界

系统目标不是把热点关键词直接转换成商品名，而是：

> 持续收集全球趋势信号，识别可能产生新实体消费品需求的变化，再用平台证据验证具体商品假设。

第一阶段只关注适合电商销售的低风险实体消费品，不包含软件、订阅、咨询、课程、资料包、危险品、医疗功效商品和未经授权的事件周边。

已确定的证据链：

```text
TrendEvent
  -> OpportunitySignal
  -> ProductHypothesis
  -> MarketEvidence
  -> ValidatedRecommendation
```

事实、判断、假设和验证结果必须分层保存。缺少证据时允许为空或弃权，不允许用规则分、模型置信度、向量相似度或假设分冒充市场需求。

产品边界详见 `docs/product-boundary-and-architecture.md`。

## 2. 当前实现状态

### 2.1 Phase 0–1：已完成

- `local-rules-v2` 主动弃权，不再从新闻关键词生成固定商品模板。
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
- OpenAI 分析路径只允许产生 OpportunitySignal；若返回旧 `OpportunityDraft` 会被拒绝。

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

- `local-rules-v2` 按设计弃权；
- 默认未配置 OpenAI OpportunityAssessment Provider；
- embedding 只用于候选特征，不允许自动生产线索；
- 真实证据覆盖不足。

## 4. 最新问题诊断：当前架构缺少两个中间层

### 4.1 正文与证据获取不足

当前 `app/evidence.py` 是通用 HTTP 抓取器：

- 不执行 JavaScript；
- 不使用登录态或 Cookie；
- `follow_redirects=False`；
- 没有微博等来源专用解析；
- 登录墙、反爬或动态页面会回退成热榜标题；
- 部分只有标题的 hotlist 证据仍会以 `valid_for_analysis=1` 进入分析。

这会导致页面看起来有多条证据，实际却只有重复标题。不能依赖大模型弥补缺失正文，否则容易用常识产生看似合理但无证据的商品方向。

建议新增 `EvidenceBundle`：

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

证据获取建议按顺序：

1. 数据源原始摘要、正文或关联新闻 URL。
2. 来源专用公开解析器。
3. 同事件的无需登录独立新闻报道。
4. 公开社区消费者讨论。
5. 人工添加 URL、粘贴正文或评论。

不要把绕过登录、验证码或付费墙作为核心能力。

### 4.2 页面缺少可读的决策解释

事件页当前直接显示：

```text
ready · model@revision · 正向相似度 · 负向相似度 · 类目候选
```

这是模型调试信息，不是用户解释。应新增确定性的解释 ViewModel，不需要大模型：

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

当前方向不应改成“只做一个 Skill/Agent”，也不应继续把所有研究判断都硬编码进定时程序。推荐采用混合架构。

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

### 5.3 推荐的新链路

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

## 6. 下一步优先级

### P0：EvidenceBundle 与证据质量

1. 区分正文、摘要、标题和消费者声音。
2. 标题证据不再按半条正文计入分析质量。
3. 保存标准化抓取失败原因。
4. 增加来源专用公开解析和关联新闻回退。
5. 支持人工补充 URL、正文和消费者评论。
6. 证据不足时停止 OpportunityAssessment，并明确列出缺失项。

### P0：事件页解释层

1. 建立 `OpportunityAssessmentView` 或等价 ViewModel。
2. 结构化展示语义指标、证据覆盖、当前决策和停止原因。
3. 将状态和标签翻译为中文。
4. 类目候选明确标记为“探索性联想”，不能与 OpportunitySignal 混在一起。

### P1：ResearchCandidate

1. 新增“待研究方向”对象，允许展示雨具、防水收纳等研究空间。
2. 必须保存产生原因、类目相似度和缺失证据。
3. ResearchCandidate 不能进入商品假设或推荐队列。

### P1：大模型 OpportunityAssessment Provider

1. 输入必须是 EvidenceBundle，而不是热榜标题。
2. 输出包含事实、推断、引用、缺失证据和弃权原因。
3. 不允许直接输出 ProductHypothesis。
4. 所有引用必须属于输入证据。
5. 低证据覆盖时强制弃权。

### P2：研究 Skill/Agent

1. 把“补充公开证据 -> 形成 Assessment -> 人工确认 -> 写回 Signal”封装成可复用 Skill。
2. Agent 使用现有 API 和未来 EvidenceBundle API。
3. Agent 输出和工具调用保留审计记录。
4. Agent 不直接发布推荐。

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
  -> 35 passed

python -m pytest -q tests\test_core.py
  -> 33 passed

python -m compileall -q app
  -> passed

python -m ruff check app tests
  -> passed

git diff --check
  -> passed，仅 Windows LF/CRLF 提示

PRAGMA foreign_key_check
  -> []
```

全量测试包含真实外部来源。单次 NewsNow/Google Trends 失败应先单独复测，不要直接判定代码回归。

## 9. 重要文件

- 产品边界：`docs/product-boundary-and-architecture.md`
- 正文抓取：`app/evidence.py`
- 数据库：`app/db.py`
- 分析 Schema 与 Provider：`app/analysis.py`
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

> 先实现 EvidenceBundle 和事件页的结构化解释，再接入基于证据的大模型 OpportunityAssessment；之后才封装研究 Skill/Agent。
