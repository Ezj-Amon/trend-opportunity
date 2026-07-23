# 业务流程合同

状态：当前业务流程与 AI 边界的唯一真相来源

适用范围：趋势采集至市场验证的节点职责、停止条件、当前实现、目标行为和差距

本文描述业务合同，不展开完整数据库字段或技术架构。模块关系见 [技术架构](architecture.md)。“当前实现”必须以可执行代码和数据库 Schema 为准；“目标行为”不得被表述为已经接入。

## 全局原则

- 趋势热度不等于消费需求，AI 判断草稿也不等于人工结论。
- 系统不要求每天产出商品、机会线索或推荐。
- 每个节点都允许停止；零 Candidate、零 Signal 和零推荐都是合法结果。
- 当前用户侧核心闭环截止到“已审核判断卡”；`OpportunitySignal` 只作为批准后的内部兼容对象，不是默认 UI 交付物。
- 越接近推荐，证据门槛越高。不得用规则分、Embedding 相似度、模型置信度或商品假设分替代市场证据。
- 大模型只能基于已保存的 EvidenceBundle 生成带引用的 OpportunityAssessment 草稿；不能自动批准、创建 Signal、输出商品名/类目/查询词/价格或强制生成推荐。

## Node 0：定时触发

- **目的**：按计划或人工请求启动一次可审计 Pipeline。
- **触发条件**：调度器启用并到达周期，或用户通过 CLI/API 显式启动；同一时刻没有其他 Pipeline 持有租约。
- **输入**：环境配置、来源列表、市场配额和研究预算。
- **处理**：创建 `pipeline_runs`，获取 `job_leases`，初始化进度；失败时保存错误和结束状态。
- **输出**：运行 ID 和运行上下文。
- **下一节点**：Node 1。
- **停止条件**：已有运行、租约获取失败、配置或启动异常。
- **是否调用大模型**：否。
- **对应代码**：`app/main.py::scheduler_loop`、`POST /api/run`；`app/cli.py::collect_pipeline`（`app.cli collect`）；`app/pipeline.py::Pipeline.run`。`app.cli run` 只启动 Web 服务，不触发本节点。
- **当前实现**：定时、CLI 和 Web API 入口均存在；调度器默认关闭。
- **目标行为**：保持单实例租约、可观测进度和显式失败。
- **当前差距**：没有独立任务队列或多节点调度；当前项目也不要求该能力。

## Node 1：趋势采集

- **目的**：获取国内外公开趋势、搜索和社区信号，并保留原始请求审计。
- **触发条件**：Node 0 成功启动。
- **输入**：NewsNow 来源 ID、Google Trends 地区和可选 Reddit OAuth 配置。
- **处理**：并发抓取来源，记录成功、状态码、延迟、错误、负载哈希和原始条目。
- **输出**：`source_snapshots` 和 `source_items`。
- **下一节点**：Node 2。
- **停止条件**：所有真实来源均失败或没有任何有效条目；单个来源失败不阻止其他来源继续。
- **是否调用大模型**：否。
- **对应代码**：`app/sources.py`；`app/pipeline.py::_persist_source_results`。
- **当前实现**：NewsNow、Google Trends RSS 已启用；Reddit 仅在凭证存在时启用。
- **目标行为**：可靠保存公开趋势事实，不把来源信号解释为销量或购买需求。
- **当前差距**：依赖外部公共源可用性；没有可靠公开小红书源，也不绕过登录或反爬。

## Node 2：标准化、聚类和去重

- **目的**：把相同或明显相近的来源条目合并为 TrendEvent，同时保存聚类依据。
- **触发条件**：Node 1 至少产生一个新条目。
- **输入**：标题、市场、语言、信号类型、时间和来源条目。
- **处理**：标准化标题；在近期事件中按市场兼容性和词面相似规则匹配；创建 `event_members`；对本轮尚未分析、没有证据和初筛记录的明显重复事件做受保护合并。
- **输出**：`trend_events`、`event_members` 和聚类分数/方法。
- **下一节点**：Node 3。
- **停止条件**：无有效条目；不满足安全合并条件的事件保持独立。
- **是否调用大模型**：否。可选 Embedding 不是大模型，也不参与此处自动合并。
- **对应代码**：`app/clustering.py`；`app/pipeline.py::_cluster_items`、`_consolidate_unanalyzed_events`；探索性重复候选在 `app/semantic_duplicates.py`。
- **当前实现**：词面聚类和受保护合并已实现；语义重复只生成人工候选。
- **目标行为**：减少明显重复，同时不破坏已有分析、证据和审核链。
- **当前差距**：主聚类仍以标题词面规则为主；跨语言和复杂语义重复需要人工复核。自动语义合并保持冻结。

## Node 3：趋势评分与 Top-N 选择

- **目的**：在有限研究预算下，按市场保留少量高优先级事件进入初筛。
- **触发条件**：Node 2 产生或触达事件。
- **输入**：事件排名历史、活跃来源、首次/最近出现时间和市场。
- **处理**：计算覆盖、排名、速度、持续性、新鲜度和总趋势分；中国与非中国市场分别按配置选择 Top-N。
- **输出**：带趋势分的 TrendEvent 和本轮 `selected_ids`。
- **下一节点**：Node 4。
- **停止条件**：本轮无事件或对应市场配额为零；未入选事件只保留事实记录。
- **是否调用大模型**：否。
- **对应代码**：`app/scoring.py::calculate_trend_scores`；`app/pipeline.py::_score_events`、`_select_events`。
- **当前实现**：已实现，且是当前真实流程中不可省略的一步。
- **目标行为**：趋势分只用于发现和预算排序，不被解释为商业价值。
- **当前差距**：Top-N 仍主要由趋势分决定，商业相关性在下一节点才判断。

## Node 4：规则初筛

- **目的**：在抓正文前排除明显不适合第一阶段实体消费品研究的事件。
- **触发条件**：事件进入 Node 3 的 Top-N；已有未完成 Candidate 也会按当前规则重筛。
- **输入**：TrendEvent 标题、来源标题/摘要、市场、信号类型和人工高风险标签。
- **处理**：检查灾难伤亡、犯罪伤害、赛事、人物八卦、软件服务、医疗功效、政治人事、短时事件、实体消费关联和持续性表达；保存输入哈希、原因和决定。
- **输出**：`eligible`、`needs_review` 或 `rejected` 的 `research_screenings`；`needs_review` 可产生一次不可改写的人工复核。
- **下一节点**：`eligible` 进入 Node 5；`needs_review` 经人工批准有限补证后进入 Node 5。
- **停止条件**：`rejected`；`needs_review` 未复核或被人工排除。停止时仍可保存标题级 Evidence 和不足 Bundle，但不抓正文。
- **是否调用大模型**：否。
- **对应代码**：`app/research_screening.py`；`app/pipeline.py::_build_research_candidate`、`collect_reviewed_screening`；`POST /api/research-screenings/{id}/review`。
- **当前实现**：规则、审计、待复核队列和一次性有限补证均已实现。
- **目标行为**：用低成本规则决定是否值得投入证据预算，不生成商品或购买需求结论。
- **当前差距**：关键词规则覆盖有限，模糊事件仍依赖人工复核。

## Node 5：最小证据采集

- **目的**：用最小公开访问预算取得足以判断事件事实的独立证据。
- **触发条件**：Node 4 为 `eligible`，或最新 `needs_review` 获人工 `collect_limited_evidence` 批准。
- **输入**：事件、来源条目已有 URL/关联新闻、当前 Evidence、搜索与页面预算。
- **处理**：先保存热榜标题为 `title_only`；依次尝试直接公开页、来源关联新闻、公共新闻搜索；逐页执行公网地址、正文真实性、长度、相关性和近重复检查；每保存一页即重算 Bundle。
- **输出**：标准化 `evidence`、`evidence_collection_runs` 和新的 EvidenceBundle 快照。
- **下一节点**：每次采集后进入 Node 6；未就绪且仍有预算时返回本节点继续。
- **停止条件**：Bundle 已 ready、预算耗尽、公开来源耗尽、抓取关闭或采集失败。登录墙、验证码、付费墙和私网目标一律停止并记录原因。
- **是否调用大模型**：否。
- **对应代码**：`app/evidence.py`、`app/evidence_collectors.py`、`app/news_search.py`；`app/pipeline.py::_build_research_candidate`；`app/research_tools.py`。
- **当前实现**：默认最多 1 次搜索、4 个页面；取得最小证据后立即停止。支持手工 Evidence 和受控 ResearchRun 工具。
- **目标行为**：通常以 1 篇完整正文加第 2 个独立有效来源结束，不追求固定篇数。
- **当前差距**：没有浏览器渲染、登录态证据或完整自主 Research Agent；只使用公开 HTTP、RSS、可选 SearXNG 和人工输入。

## Node 6：EvidenceBundle 就绪判断

- **目的**：形成不可变证据快照，并确定是否具备结构化机会判断的最低事实基础。
- **触发条件**：Evidence 集合发生变化，或用户/API 显式重建 Bundle。
- **输入**：当前事件的 EvidenceItem 集合。
- **处理**：分类正文、官方公告、摘要、消费者讨论、搜索摘要和标题；按注册域名与近重复内容计算独立来源；记录失败和缺失证据。
- **输出**：`insufficient`、`partial` 或 `ready_for_assessment` 的 `evidence_bundles`。业务关键字段包括输入哈希、证据 ID、内容计数、独立来源数、诊断质量分和缺失证据。
- **下一节点**：具备 Candidate 条件时进入 Node 7。
- **停止条件**：至少两个独立有效来源且至少一篇完整正文或官方公告时为 ready；否则可以停在 `partial`/`insufficient`。消费者声音缺失不阻止 ready，但必须显式记录。
- **是否调用大模型**：否。
- **对应代码**：`app/evidence_bundle.py::build_evidence_bundle`、`persist_evidence_bundle`；Evidence Bundle API 位于 `app/main.py`。
- **当前实现**：硬门槛不依赖 `EVIDENCE_READY_SCORE`；质量分只作诊断和显示。
- **目标行为**：以来源独立性和至少一条强正文为准，允许可靠摘要作为第二来源。
- **当前差距**：配置项 `EVIDENCE_READY_SCORE` 仍保留兼容，但不参与 ready 决策，容易造成配置含义误解。

## Node 7：ResearchCandidate

- **目的**：保存值得进一步判断的事件方向；Candidate 不是 OpportunitySignal，也不是商品结论。
- **触发条件**：事件通过安全门，且 Bundle 至少为 `partial`；若语义特征 ready，证据不足时也可形成探索性 Candidate。
- **输入**：TrendEvent、EvidenceBundle、可选语义特征和人工标签。
- **处理**：生成研究原因、研究问题、缺失证据和优先级；兼容类目字段可以为空且不得暴露到默认 UI；新 Bundle/版本可 supersede 未完成旧 Candidate。
- **输出**：`research_candidates`，初始状态为 `pending`。
- **下一节点**：当前自动 Pipeline 在此结束。后续显式启动 ResearchRun，补证或进入 Node 8。
- **停止条件**：安全门阻断；无可用语义特征且 Bundle 为纯标题 `insufficient`；人工不继续；Candidate 被新版本替换。
- **是否调用大模型**：否。可选 Embedding 只用于探索性检索。
- **对应代码**：`app/research_candidates.py::candidate_from_event`、`persist_research_candidate`、`transition_research_candidate`；`app/pipeline.py::_persist_semantic_features`；Candidate API 位于 `app/main.py`。
- **当前实现**：已实现。Pipeline 不会越过此节点自动创建 ResearchRun、Assessment 或 Signal。
- **目标行为**：成为 AI 草稿或人工判断的受控入口，并完整保存证据版本。
- **当前差距**：当前没有自动 Research Agent 调度；真实运行可能合法地产生零活跃 Candidate。

## Node 8：AI OpportunityAssessment v2 三级判断卡

- **目的**：让模型只基于已保存 EvidenceBundle 生成结构化、带引用、可弃权的三级判断草稿，降低非专业用户的判断难度。
- **触发条件**：显式请求云端 Assessment；已配置 API Key；Candidate 有已完成 ResearchRun；Bundle ready；事件未命中商业研究安全门。
- **输入**：事件摘要、Bundle 指标、Bundle 内 Evidence 摘录和证据 ID。
- **处理**：调用 Structured Outputs；区分事实与推断；固定输出消费变化 `related/unrelated/uncertain`、新问题 `clear/needs_evidence/none`、研究建议 `continue_research/defer/abandon`，并整理变化、单一主要用户、场景/约束、现有解决方式、解决缺口、未满足需求、持续性、交付周期和缺失证据。三级依据及事实/推断引用必须属于当前 Event 和 Bundle。
- **输出**：`generation_status=completed` 且 `review_status=pending` 的 `opportunity_assessments` v2 草稿。总体状态只能按三级判断派生：`related + clear + continue_research` 为 `worth_following`；任一级不确定或需补证为 `insufficient_evidence`；不相关、无问题或放弃为 `abstained`。
- **下一节点**：Node 9。
- **停止条件**：Bundle 不 ready、敏感事件、未配置模型、调用失败、Schema 失败、输出商品内容或引用非法；不得回退为本地商品模板。技术失败保存为不可审核的失败审计，可安全重试。
- **是否调用大模型**：是，且这是当前业务代码中唯一云端大模型职责。
- **对应代码**：`app/opportunity_assessment.py::CloudOpportunityAssessmentProvider`；`POST /api/research-candidates/{id}/assessments/cloud`；工作台薄编排 `POST /api/workbench/research-candidates/{id}/ai-draft`。
- **当前实现**：Provider、v2 Schema、总体状态一致性、禁止商品输出、引用校验、失败审计和重试已实现；`/workbench` 对 ready Candidate 提供显式生成按钮，只保存待审核草稿。历史 v1 记录只读展示，不补造 v2 字段。
- **目标行为**：保持 AI 草稿生成与人工审核分离，页面不得把草稿解释为最终结论。
- **当前差距**：Pipeline 仍不调用模型；没有完整自主 Agent，也没有真实模型灰度运行样本。

## Node 9：人工确认、补证与已审核判断卡

- **目的**：让人只核对事实、问题和持续性，并保存继续研究、补证或放弃的不可改写决定。
- **触发条件**：存在 `generation_status=completed`、`review_status=pending` 的 OpportunityAssessment v2。
- **输入**：Assessment、Candidate、EvidenceBundle、Evidence 引用；三项人工确认；最终动作、原因代码、选中的缺失证据和备注。
- **处理**：事实选择准确/不准确/不确定，问题选择真实/存疑/不确定，持续性选择足够/不足/不确定。只有 AI 建议 `continue_research` 且三项均为肯定时允许批准；AI 未建议继续时只能补证重判或放弃。审核决定不可改写。
- **输出**：`approved`、`needs_more_evidence` 或 `rejected` 的已审核判断卡。批准时仍创建唯一内部 `opportunity_signals` 兼容对象，但默认 UI 不展示 Signal 或下游入口。
- **下一节点**：补证时经工作台接口原子保存人工 Evidence、重建不可变 Bundle、创建继任 Candidate，并将旧 Candidate/Assessment 标记为已替代；新 Bundle ready 后回到 Node 8。批准或放弃后进入判断记录并停止用户流程。
- **停止条件**：Assessment 技术失败、Bundle 不 ready、引用无效、人工放弃或要求补证。系统不要求形成 Signal。
- **是否调用大模型**：审核本身否。
- **对应代码**：`app/main.py::review_opportunity_assessment`、`supplement_workbench_evidence`、`_create_signal_from_assessment`；`POST /api/opportunity-assessments/{id}/review`；`POST /api/workbench/research-candidates/{id}/evidence`。
- **当前实现**：三级确认、动作约束、不可改写审核、补证版本链和内部 Signal 兼容门已实现。旧人工快捷 API 仍保留，但事件详情已移除旧完整 Assessment 表单和下游 CTA。
- **目标行为**：已实现当前最小闭环；下一步只需用真实模型做灰度样本验证，不扩展下游产品流程。
- **当前差距**：尚无真实模型端到端样本；当前不实现每日硬配额。

## Node 10：ProductHypothesis

> 兼容边界：本节点不属于当前默认用户流程，默认导航与交叉链接已隐藏；旧路由/API 仅供直接 URL 回滚。

- **目的**：把已确认消费变化转化为少量、可验证的实体商品方向；它仍不是推荐。
- **触发条件**：Signal 来自 approved、`worth_following` Assessment；Candidate completed；Bundle ready；Signal 状态允许继续。
- **输入**：OpportunitySignal、上游 Evidence、人工填写的实体形态、用户、场景、问题、差异和查询词。
- **处理**：校验证据继承、实体商品边界、目标 Amazon 站点、风险和每个 Signal 的有效方向数量；由人工审核进入 `ready_for_validation`。
- **输出**：`product_hypotheses` 和审核反馈。
- **下一节点**：Node 11。
- **停止条件**：没有批准 Signal、非实体商品、阻断风险、查询词未准备、人工否决或达到数量限制。
- **是否调用大模型**：否。当前只有 `HumanProductHypothesisGenerator`。
- **对应代码**：`app/product_hypotheses.py`；`POST /api/opportunity-signals/{id}/product-hypotheses`；`POST /api/product-hypotheses/{id}/review`。
- **当前实现**：人工表单、结构校验、风险门和审核已实现。
- **目标行为**：只从已批准 Signal 形成少量商品方向，并继承可追溯证据。
- **当前差距**：没有商品方向生成模型；这不是第一阶段核心闭环的阻塞项。

## Node 11：市场验证与 ValidatedRecommendation

> 兼容边界：本节点不属于当前默认用户流程，本轮无需上传 Seller Central CSV；旧路由/API 仅供直接 URL 回滚。

- **目的**：用平台需求、竞争、单位经济、执行和证据完整度验证商品方向，并仅对完整结果形成推荐。
- **触发条件**：ProductHypothesis 已审核为 `ready_for_validation`，目标站点和查询词满足要求。
- **输入**：Seller Central Product Opportunity Explorer/Brand Analytics CSV，或人工补充的单位经济和执行证据。
- **处理**：Provider 解析并保存独立 MarketEvidence；未知维度保持空；确定性计算市场分，检查证据完整性、单位经济和风险门。
- **输出**：`market_evidence`；全部条件通过时生成 `validated_recommendations`。
- **下一节点**：验证不完整时停留或补证；完成后进入已验证选品展示。
- **停止条件**：任何关键维度缺失、单位经济或证据评分未达门槛、阻断风险、查询词不匹配或人工停止。系统不要求形成推荐。
- **是否调用大模型**：否。
- **对应代码**：`app/market_evidence.py`、`app/amazon_validation.py`、`app/reports.py`；ProductHypothesis MarketEvidence 和推荐 API 位于 `app/main.py`。
- **当前实现**：Seller Central CSV Provider、人工 MarketEvidence、复合证据和推荐资格检查已实现。
- **目标行为**：每条推荐完整回溯到 Event、Signal、Hypothesis 和 MarketEvidence；缺少市场数据时保持待验证。
- **当前差距**：需要人工取得平台数据和补充经济性证据；没有 SP-API 自动接入。
