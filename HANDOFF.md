# 项目交接：全球趋势驱动新品机会系统

更新时间：2026-07-18
工作目录：`D:\code\xpzs`
当前分支：`agent/research-architecture`
当前 HEAD：`bcf8c57 Implement evidence-backed research architecture`
Git 状态：当前分支已推送到 `origin/agent/research-architecture`，但尚未合并到 `main`，也未创建 PR；本轮二级正文与独立来源采集修复、测试和文档修改尚未提交。不要擅自提交、重置或推送。

## 1. 产品目标、唯一主链与本轮审查结论

系统最终不是交付新闻正文、趋势榜或模型分数，而是：

> 从全球趋势中筛出可能持续影响实体消费行为的变化，形成少量可验证的商品方向，再用平台需求、竞争、成本和经济性证据得到选品结论。

第一阶段只关注适合电商销售的低风险实体消费品，不包含软件、订阅、咨询、课程、资料包、危险品、医疗功效商品、灾难/伤亡商业化、赛事输赢、人物八卦和未经授权的事件周边。

面向用户的唯一主链确定为：

```text
趋势发现
  -> 机会判断
  -> 商品构思
  -> 市场验证
  -> 已验证选品
```

内部数据对象继续分层保存：

```text
TrendEvent
  -> Evidence / EvidenceBundle
  -> ResearchCandidate / ResearchRun / OpportunityAssessment
  -> OpportunitySignal
  -> ProductHypothesis
  -> MarketEvidence
  -> ValidatedRecommendation
```

EvidenceBundle、ResearchCandidate、ResearchRun、OpportunityAssessment、Agent、模型和工具调用属于“机会判断”的内部支撑，不再与五个用户业务阶段平级。事实、判断、假设和验证结果必须分层；缺少证据时允许为空或弃权，不允许用趋势分、规则分、模型置信度、向量相似度或假设分冒充市场需求。

### 1.1 2026-07-18 全面审查结论

当前数据模型基本能承载正确目标，但运行和页面没有稳定沿着主链前进：

1. 重点事件主要按趋势分进入研究，商业相关性、安全性、持续性和供应周期筛选发生得过晚。
2. 单事件允许抓取最多 15 个页面，达到证据门槛后不会立即停止，真实运行已出现单事件 10–13 篇完整正文。
3. 9 个 ResearchCandidate 中，优先级最高的是“重庆山体崩塌已搜救出8名被困者”，第二是世界杯决赛，另外包含 Grok、GitHub Copilot SDK、代码学习仓库和 PostHog。它们可以是高热趋势，但多数不应占用第一阶段实体选品研究队列。
4. 数据库已有 9 个 ResearchCandidate，但 ResearchRun、OpportunityAssessment、OpportunitySignal、ProductHypothesis、MarketEvidence 和 ValidatedRecommendation 都是 0，真实价值链停在“待研究候选”。
5. 页面中的“启动人工 ResearchRun”只创建运行记录，没有完成工具补证、结束研究和结构化 OpportunityAssessment 的完整交互。
6. 主导航和页面大量显示 EvidenceBundle、ResearchRun、语义差值、engine、model、version、HTTP 错误和请求哈希，内部工程概念压过了用户任务。
7. 新旧商品机会和市场验证链仍同时展示，增加了结果解释和维护成本。

准确判断不是“项目完全走错”，而是：**正确的业务骨架上叠加了过重的研究工程层，而且从证据到机会判断的页面闭环尚未完成。**

### 1.2 正文证据的权威停止规则

用户已确认一个话题有 1–2 篇正文即可。后续实现统一遵循：

- 默认至少 2 个独立来源；
- 至少 1 篇可分析的完整正文或官方公告；
- 第二来源可以是正文、可靠摘要或官方信息；
- 两个来源没有明显事实冲突；
- 正文与话题相关并通过模板污染、来源和近重复检查；
- 达到上述条件后立即停止新闻正文补抓，不以 10 篇正文为目标；
- 只有事实冲突、高风险需官方确认、正文仍无法判断消费场景或用户明确要求深度研究时才扩大预算；
- 消费者声音放在初步判断存在消费机会之后，不作为每个趋势抓取正文的前置强制条件。

### 1.3 抓取前的快速初筛

深度抓取前必须先低成本判断：

1. 是否涉及实体消费行为、环境或生活方式变化；
2. 是否只是人物、赛事、灾难、案件、政治或一次性新闻；
3. 变化能否持续到商品完成开发、生产和运输；
4. 是否存在可描述的用户、场景或新约束；
5. 是否命中医疗、危险、侵权或其他阻断风险；
6. 是否与已处理趋势重复。

初筛只决定是否值得花费研究预算，不生成商品名，也不声称存在购买需求。

### 1.4 程序、Agent 与搜索 MCP 的边界

- Agent 负责决定搜什么、选择哪个来源、是否需要第二篇、是否存在冲突、何时停止，并生成带引用的机会判断草稿。
- 程序负责安全访问、正文抽取、去重、来源身份、证据保存、状态转换、引用校验、风险门和可追溯性。
- 搜索 MCP 只提供候选信息和页面，不是唯一事实库，也不能独自完成选品。
- 平台数据仍是最终选品不可省略的验证层；新闻搜索只能支持趋势事实和消费变化判断。

推荐运行方式是：**程序先做快速初筛，Agent 对通过初筛的少量事件执行有预算、可停止的搜索，程序保存和校验证据。**

### 1.5 页面和中文文案基线

主导航只保留：趋势发现、机会判断、商品方向、市场验证、已验证选品。数据源健康、运行记录、语义评测、Agent 配置和审计日志进入“系统状态/管理”二级入口。

事件页默认顺序：一句话结论、当前阶段、已采用的 1–2 条关键证据、机会判断/下一步、折叠的未采用来源、折叠的技术审计。用户截图中的“抓取失败原因”应改成类似“未采用的来源（8/20；已成功采用 10 条，不影响当前证据判断）”，避免把部分失败展示成全部失败。

用户页面统一使用以下称呼：

| 内部名称 | 页面名称 |
|---|---|
| EvidenceBundle | 证据状态 |
| ResearchCandidate | 待判断趋势 |
| ResearchRun | 研究任务 |
| OpportunityAssessment | 机会判断草稿 |
| OpportunitySignal | 已确认机会 |
| ProductHypothesis | 商品方向 |
| MarketEvidence | 市场验证证据 |
| ValidatedRecommendation | 已验证选品 |

状态和错误默认中文化：`ready_for_assessment` 显示“证据够用，可以判断”，`partial` 显示“已有正文，还需 1 个独立来源”，`content too short` 显示“页面只有标题或简短摘要，未采用”，403 显示“该站点限制公开访问，已跳过”，404 显示“页面已失效，已跳过”。HTTP 状态码、原始 error、engine、model、version、request hash、延迟、环境变量和 JSON 只放入技术详情。

### 1.6 文档规则

不再为本轮审查新建平行文档；当前产品结论、真实状态、问题和下一步统一维护在 `HANDOFF.md`。`README.md` 只负责项目入口和启动方式，`docs/product-boundary-and-architecture.md`、`docs/research-agent-architecture.md`、`docs/research-agent-implementation-plan.md` 仅作为历史设计和实现细节参考。若内容冲突，以本文件第 1 节和第 6 节为当前执行基线。

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
- 兼容迁移会重新验证旧文章正文；只有通过长度、模板污染和正文相关性校验的内容才保留为 `full_article`/`article_summary`，hotlist、搜索页和伪正文降级为不可分析的 `title_only`。
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
- 二级新闻补证已接入：Trafilatura 抽取正文，Google News RSS + `googlenewsdecoder` 作为默认无密钥搜索，SearXNG 作为可选第二 Provider；原站直链仍逐跳执行 SSRF 校验。
- 独立来源按 Public Suffix List 的注册域计数；同域子站和跨域近重复转载不会被重复计算，纯标题也不再贡献独立来源数。

本轮 Candidate 入口修复与真实运行结果：

- `research-candidate-v2` 已允许默认 `ENABLE_EMBEDDINGS=false` 时，为安全且至少 `partial` 的 Bundle 创建无类目 Candidate；`semantic_feature_id` 保持为空，避免以后启用模型、替换 disabled/unavailable 特征时触发外键冲突。
- 纯标题 `insufficient` Bundle 在没有 ready 语义特征时仍显式弃权；Candidate 只保存研究问题和缺失证据，不生成商品名、查询词或需求结论。
- 最新真实 Pipeline 已成功创建 9 个 ResearchCandidate，其中 5 个 `ready_for_assessment`、4 个 `partial`；说明正文补证入口已经生效。
- 最新结果同时暴露了更重要的问题：候选包含山体崩塌、世界杯、软件/代码项目，且多个事件抓到 10–13 篇正文。后续优先级已从“继续提高正文数量”切换为“抓取前初筛、1–2 篇即停和机会判断页面闭环”。

## 3. 真实数据库状态

当前 `data/trends.db`（2026-07-18 最新真实运行后）：

```text
trend_events                    1415
evidence                         156
evidence_bundles                  94
opportunity_signals                0
semantic_event_features           24
semantic_evaluation_labels         7
semantic_duplicate_candidates     11
research_candidates                9
research_runs                      0
research_tool_calls                0
opportunity_assessments            0
product_hypotheses                 0
market_evidence                    0
validated_recommendations          0
pipeline_runs                      16
legacy product_opportunities       34
legacy market_validations          12
```

当前证据和 Bundle 分布：

```text
full_article ready                 58
article_summary ready               2
title_only ready                     9
content_too_short                   67
robots_or_access_denied              9
http_error                           6
content_irrelevant                   4

EvidenceBundle insufficient         61
EvidenceBundle partial              28
EvidenceBundle ready_for_assessment  5
```

零 OpportunitySignal、零商品假设和零推荐仍是合法结果，但已经不再能只用“缺少正文”解释。当前主要原因是：

- 定时 Pipeline 按设计只创建 ResearchCandidate，不自动创建 OpportunitySignal；
- 默认未配置 OpenAI OpportunityAssessment Provider；
- embedding 只用于候选特征，不允许自动生产线索；
- 页面没有从 ResearchRun 到 OpportunityAssessment 的完整操作闭环；
- 候选初筛不准确，研究预算被高热但不适合选品的事件占用。

## 4. 问题诊断与已实现解法

### 4.1 正文与证据获取不足

`app/evidence.py` 和 Evidence Collector 现在：

- 不执行 JavaScript；
- 不使用登录态或 Cookie；
- 手工跟随有限次重定向，并对每一跳重新执行公网/SSRF 校验；
- 优先使用成熟的 Trafilatura 抽取正文，并保留 meta、Open Graph、JSON-LD 和普通正文作为回退；
- 对正文执行最小长度、站点模板污染、搜索/热榜页和事件相关性校验，摘要不会升级为完整正文；
- 默认使用 Google News RSS 主动寻找无需登录的独立报道并解码到原站直链，可选并行使用自建 SearXNG；
- 独立来源按注册域名去重，跨域近重复转载也只计一次；
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

### 4.2 页面已有解释结构，但仍不符合用户任务

事件页已经使用确定性的 `EventResearchView`，能够显示结论、证据计数、缺失项、失败原因和下一步；但本轮截图与模板审查确认，当前信息顺序仍会误导用户：

- “完整正文 10”下方直接突出很长的“抓取失败原因”，成功采用的正文没有先展示，用户容易理解成全部失败；
- ResearchCandidate、ResearchRun、OpportunityAssessment、语义特征、engine/model/version 和原始英文错误直接暴露；
- 页面把运行预算、工具审计和技术状态放在业务决策主链中；
- “启动人工 ResearchRun”之后缺少完成机会判断的页面操作；
- 人工创建 Signal 和商品假设依赖连续 prompt，并要求用户手填证据 ID；
- 旧版商品机会和验证卡仍与新主链同时出现。

后续页面必须先展示结论、当前阶段、已采用的 1–2 条关键证据和唯一下一步；未采用来源与技术审计默认折叠。详细中文命名和页面顺序以第 1.5 节为准。

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

正文真实性、主动公共新闻搜索和独立来源去重已经生效。当前优先级不再是继续增加抓取量，而是让系统选对研究对象、证据够用即停并真正完成一次机会判断。

### P0：抓取前快速初筛

1. 在任何正文深度抓取前检查实体消费关联、持续性、交付周期、安全风险和重复事件。
2. 明确排除灾难、伤亡、案件、赛事、人物、软件服务和代码项目。
3. 不再仅按趋势分决定 ResearchCandidate；趋势热度只作为发现和排序的一部分。
4. 初筛结果必须可解释，但不能生成商品名或购买需求结论。

### P0：每话题 1–2 篇正文，够用即停

1. 默认门槛为 2 个独立来源、至少 1 篇完整正文或官方公告。
2. 每抓取成功一个来源就重算 EvidenceBundle，而不是完成全部预算后才计算。
3. 达标后立即停止后续正文搜索；默认最多 1 次搜索、3–4 个候选页面、2 篇成功正文。
4. 只有事实冲突、高风险官方确认、仍无法判断消费场景或用户明确要求时扩大预算。
5. 记录“为什么已经停止”，不只记录抓取失败。

### P0：完成机会判断页面闭环

1. 把 ResearchRun 和 OpportunityAssessment 封装成一个“机会判断”用户任务。
2. 页面内提供 Agent 草稿或结构化人工表单，并允许从已采用证据中勾选引用。
3. 页面内完成“值得跟进 / 不适合选品 / 需要补证据”，不要求用户手填证据 ID。
4. 真实完成至少 1 条 Candidate -> Assessment -> Signal 样本，再评估批量运营。

### P1：简化信息架构和中文文案

1. 主导航收敛到五个业务阶段；语义评测、运行日志和 Agent 配置退出主导航。
2. 成功采用的证据优先展示，失败来源和技术审计默认折叠。
3. 所有状态、空页面和错误信息使用中文，并给出唯一下一步。
4. ResearchCandidate 与 OpportunityAssessment 在页面上统一归入“机会判断”。

### P2：商品方向到市场验证闭环

1. 只从已确认 OpportunitySignal 生成少量 ProductHypothesis。
2. 商品方向审核后进入唯一 MarketEvidence 链。
3. 旧 `product_opportunities`、旧 `market_validations` 和旧版页面退出用户主流程。
4. 只有市场证据、风险和经济性门槛全部通过，才生成 ValidatedRecommendation。

### P3：可选 Agent 与执行基础设施

1. Agent 只处理通过初筛且证据不足的少量事件。
2. MCP 继续作为可选工具接口，不成为产品架构中心或唯一事实来源。
3. 只有出现明确跨进程需求时才增加独立 worker；继续复用 Candidate 租约、预算和受控工具接口。
4. 只有经授权且合规需求明确时评估浏览器证据；不得持久化 Cookie 或登录页。

## 7. 验收标准

- 用户能用一句话说清系统最终产出是“已验证选品”，不是新闻或模型分数。
- 灾难、赛事和软件热点在正文深度抓取前退出选品研究队列。
- 一个话题有 1 篇正文和第 2 个独立来源后默认停止。
- 用户不理解 EvidenceBundle、ResearchRun、HTTP 和模型版本也能完成机会判断。
- 页面先显示已成功采用的关键证据，未采用来源不会被误解成全部失败。
- 从待判断趋势到已确认机会可以在页面内完整完成。
- Agent 的每个事实和判断都有可回溯引用，Agent 中断不影响数据库状态。
- 没有平台证据时，任何页面都不会称其为最终选品推荐。
- 最终推荐仍能完整回溯 TrendEvent -> OpportunitySignal -> ProductHypothesis -> MarketEvidence -> ValidatedRecommendation。

## 8. 验证状态

最后完整验证：

```text
uv run pytest -q
  -> 63 passed，1 个上游 TestClient 弃用警告

python -m compileall -q app
  -> passed

ruff check app tests
  -> passed

git diff --check
  -> passed，仅 Windows LF/CRLF 提示

PRAGMA foreign_key_check
  -> []

Skill quick_validate
  -> Skill is valid!
```

上述 63 项测试、迁移副本和“沈阳暴雨”联网样本是在最新真实 Pipeline 运行前完成的工程验证。此后真实 `data/trends.db` 已重新采集，当前状态以第 3 节为准：58 条 ready 完整正文、94 个 Bundle、5 个 ready Bundle、9 个 Candidate，仍无 Run、Assessment、Signal 或下游选品对象。

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

## 10. 架构符合性与未解决问题

### 10.1 总体结论

静态结构基本符合 `docs/research-agent-architecture.md` 和 `docs/research-agent-implementation-plan.md`：EvidenceBundle、ResearchCandidate、ResearchRun、OpportunityAssessment、OpportunitySignal 已分层；Bundle 和 Assessment 使用快照；引用、风险门、人工审核、受控工具、租约与幂等均已落地；定时 Pipeline 不会自动创建 Signal、商品假设或推荐；MCP 和浏览器登录态没有成为核心依赖。

默认 `ENABLE_EMBEDDINGS=false` 的 Bundle -> Candidate 已通过真实运行验收，真实库已有 9 个 Candidate；但仍没有 Candidate -> Run -> Assessment -> Signal 人工样本。当前更准确的状态是：**证据抓取和 Candidate 入口已贯通，但候选初筛、抓取早停和真实机会判断闭环仍未完成。**

### 10.2 已解决：默认配置可以进入 ResearchCandidate

实现：`candidate_from_event()` 在语义特征未启用或不可用时，允许安全且至少 `partial` 的 Bundle 形成无类目 Candidate；纯标题 `insufficient` Bundle 继续弃权。降级 Candidate 不引用可替换的 disabled/unavailable 特征行，后续 ready 特征会生成新版本 Candidate 并 supersede 旧候选。

证据：默认 Pipeline 回归测试直接断言 `disabled` 特征仍创建无类目 Candidate、页面显示非结论提示、后续 ready 特征可安全替换并 supersede；真实数据库副本的实际 Pipeline Candidate 阶段也创建成功，未创建 Signal，外键检查通过。

剩余事项：`/research` 已有 9 个候选，但候选质量不符合产品目标，且页面不能独立完成机会判断。问题已从“入口不可达”转为“选错对象、抓取过量和操作闭环缺失”。

### 10.3 已解决：二级正文真实性与主动独立来源采集

旧问题有两部分：一是 B 站搜索页、酷安模板页被误判为完整正文；二是 `RelatedNewsCollector` 只消费上游已有 URL，没有主动寻找独立报道。

实现：Trafilatura 正文抽取 + 内容真实性/相关性校验；Google News RSS 默认搜索 + 原站链接解码；可选 SearXNG；注册域和近重复正文去重；Pipeline 与受控 `collect_related_news` 共用同一 Collector 和预算。

证据：63 项回归通过；真实数据库副本降级 7 条伪正文；真实联网样本成功抓到独立原站正文；最新真实运行累计 58 条 ready 完整正文并形成 5 个 ready Bundle。剩余瓶颈不再是正文数量，而是抓取前初筛、达到 1–2 篇后早停和真实 OpportunityAssessment 样本。

### 10.4 P1：采集成功、证据质量和研究就绪状态没有统一监控

现状：真实库已有 16 次 Pipeline Run、94 个 Bundle、5 个 `ready_for_assessment` Bundle 和 9 个 Candidate。Dashboard 的“来源健康”仍主要看主源请求是否成功，不能表达候选是否适合选品、是否抓取过量、是否完成机会判断或是否产生下游对象。

性能风险：最新一轮多个主源延迟较高，例如百度约 24 秒、B 站 36 秒、酷安 39 秒、贴吧 43 秒、Google Trends DE/GB/US 约 47/55/59 秒、Hacker News 51 秒。当前没有按阶段区分的超时率或质量 SLO。

历史状态混淆：`/healthz` 顶层已报告新 Pipeline 模式，但 `latest_run.config_json` 和 `legacy_latest_analysis` 仍会展示数据库中的历史 `local-rules` / `local-rules-v1`，容易被误解为旧规则仍在活跃运行。活跃代码已删除，问题在于监控语义和历史字段未隔离。

### 10.5 P1：人工研究工作台只完成展示和启动，操作闭环仍依赖 API/Skill

现状：`/research` 和事件页可以启动人工 ResearchRun、显示工具审计、展示并审核已有 Assessment；但页面没有执行受控工具、添加人工 Evidence、完成 Run、创建人工 Assessment 或触发云端 Assessment 的操作入口。

影响：后端 API 和 Skill 具备这些能力，但普通用户仅靠页面无法完成实施计划所述的人工研究闭环。真实库中仍是 0 Run、0 ToolCall、0 Assessment，说明这条链尚未经过真实操作验收。

### 10.6 P1：新研究状态机仍有兼容旁路

Candidate 状态接口目前允许把非 `superseded` Candidate 直接更新为任一合法状态，没有校验允许的状态转换，也没有在写入 `evidence_ready`、`awaiting_review` 或 `completed` 时复核 Bundle 和 Assessment 条件。

事件页和 `POST /api/events/{event_id}/opportunity-signals` 仍支持直接人工创建 Signal；它只要求证据 ID 属于事件，不要求有效正文、ready Bundle、Candidate 或批准的 OpportunityAssessment。这是 Phase 1 人工工作台的兼容路径，但意味着目标架构中“只有人工确认 `worth_following` Assessment 后才创建 Signal”尚不是唯一受控路径。

需要后续明确：这些接口是受信任管理员的显式旁路，还是应收紧到新状态机；在决定前不能宣称所有新 Signal 都能回溯完整 Research 链。

### 10.7 P2：Agent 与浏览器配置尚未接入执行器（搜索已接入）

当前受控工具仍是 `get_context`、`fetch_public_page`、`collect_related_news` 和 `rebuild_evidence_bundle`，但 `collect_related_news` 已使用 `RESEARCH_MAX_SEARCH_QUERIES` 执行主动公共新闻搜索。仍没有浏览器工具或独立 Research Agent worker；`ENABLE_RESEARCH_AGENT`、`ENABLE_BROWSER_EVIDENCE` 和 `RESEARCH_MAX_BROWSER_PAGES` 仍是预留配置，`AbstainingRulesAssessmentProvider` 也没有接入 API 或 Pipeline。

这些能力在实施计划中属于默认关闭或第二版可选项，不阻塞最小人工闭环，但配置项会让使用者误以为开启后已有实际执行能力。后续应实现、隐藏或明确标记为预留。

### 10.8 P2：新主链仍受旧数据模型约束

`opportunity_signals.analysis_id` 仍是非空外键。批准 OpportunityAssessment 或人工创建 Signal 时，代码会额外插入兼容用 `pipeline_runs` 和 `analyses` 记录，再创建 Signal。旧 `product_opportunities`、`market_validations`、旧页面/API 和 `/healthz` 的 legacy 字段也仍保留。

这保证旧数据可用，但增加了双轨查询、清理顺序和状态解释成本。长期架构若要完全独立，需要迁移 `analysis_id` 约束并明确旧表/API 的退役条件；当前仅做到“新推荐不再由旧链生产”。

### 10.9 P2：生产安全边界尚未完成

写 API 当前使用浏览器 Origin 校验、本机限制或单一 `ADMIN_TOKEN`，README 已明确这是 Demo 级保护。没有正式用户身份、角色权限、审计主体和限流；适合本机或可信内网，不适合直接公网部署。浏览器登录态、Cookie 和验证码绕过仍按设计不实现。

### 10.10 P2：验证、文档与发布状态仍有缺口

- 63 个测试和外键检查均通过，真实数据库副本已验证 v2 证据迁移，联网样本已验证主动搜索与正文抓取；但仍没有真实 Candidate -> Run -> Assessment -> Signal 端到端样本，云端 Provider 只通过 fake client 验证，没有真实模型灰度样本。
- 测试仍有 1 个 Starlette `TestClient` 上游弃用警告，不影响当前结果，但应在依赖升级时处理。
- `docs/research-agent-implementation-plan.md` 的实现说明和正文元数据已统一为“核心实施完成，运行验收进行中”。
- 代码提交 `bcf8c57` 已推送到 `origin/agent/research-architecture`，尚未创建 PR，也未合并到 `main`；本轮 `HANDOFF.md` 问题盘点尚未提交。

## 11. 绝对约束

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

## 12. 新会话建议先执行

```powershell
Set-Location -LiteralPath 'D:\code\xpzs'
git status --short --branch
git log -3 --oneline
Get-Content -LiteralPath 'HANDOFF.md' -Raw
uv run python -m compileall -q app
uv run pytest -q tests\test_core.py
```

最合理的续做入口是：

> 显式重跑 Pipeline 或实现可审计安全回填，使真实 `/research` 队列出现 Candidate；随后补齐人工研究工作台或严格按 Skill/API 完成一个真实 Run 与 OpportunityAssessment 样本。在证据质量和样本验收前保持 `ENABLE_RESEARCH_AGENT=false`，不要自动生产 Signal。
