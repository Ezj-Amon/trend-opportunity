# 技术架构

状态：当前技术结构的唯一真相来源

适用范围：模块关系、运行边界、数据对象关系、兼容结构和冻结能力

本文描述系统如何组织，不重新定义逐节点业务合同。业务触发、输入、输出、停止条件和 AI 边界见 [业务流程合同](workflow-contract.md)。当前实现事实最终以可执行代码和 `app/db.py` 中的数据库 Schema 为准。

## 1. 模块关系

系统是一个 FastAPI + SQLite 的单体应用，核心职责分为五组：

```text
来源适配与 Pipeline
  ├─ sources / clustering / scoring / screening
  ├─ evidence / collectors / bundle
  └─ semantic（可选探索性能力）

研究与判断
  ├─ research_candidates
  ├─ research / research_tools
  └─ opportunity_assessment

业务对象
  ├─ opportunity_signals
  ├─ product_hypotheses
  ├─ market_evidence
  └─ validated_recommendations（由 main/reports 读取）

Web/API
  ├─ main.py
  ├─ templates
  └─ static

持久化与审计
  └─ db.py / SQLite
```

主要文件职责：

- `app/pipeline.py`：一次自动运行的编排、租约、进度和阶段推进。
- `app/sources.py`：NewsNow、Google Trends RSS 和可选 Reddit 来源适配。
- `app/clustering.py`、`app/scoring.py`：词面聚类和确定性趋势评分。
- `app/research_screening.py`：抓取前规则初筛及人工复核记录。
- `app/evidence.py`、`app/evidence_collectors.py`、`app/news_search.py`：公开页面获取、正文校验、来源路由和公共新闻搜索。
- `app/evidence_bundle.py`：Evidence 类型、独立来源和不可变 Bundle 快照。
- `app/research_candidates.py`、`app/research.py`、`app/research_tools.py`：Candidate 状态、ResearchRun、预算、租约和受控工具审计。
- `app/opportunity_assessment.py`：人工与云端 Assessment Provider、Schema 和引用校验。
- `app/product_hypotheses.py`、`app/market_evidence.py`、`app/amazon_validation.py`：商品方向和市场验证。
- `app/main.py`：FastAPI 生命周期、页面 ViewModel 组装、API、审核状态门和下游写入。
- `app/db.py`：SQLite Schema、迁移、连接、事务辅助、租约和派生数据清理。

## 2. Pipeline

`Pipeline.run()` 是自动流程唯一编排入口。它使用进程内异步锁和 SQLite `job_leases` 防止重复运行，并把运行阶段、计数、错误和配置快照写入 `pipeline_runs`。

Pipeline 当前负责：

- 并发采集并保存来源快照和条目；
- 标准化标题、聚类和受保护的重复事件合并；
- 计算趋势分并按中国/海外配额选择事件；
- 重筛旧的未完成 Candidate；
- 执行规则初筛、有限证据采集和 Bundle 重建；
- 可选保存 Embedding 特征和语义重复候选；
- 创建 ResearchCandidate。

Pipeline 在 ResearchCandidate 处结束。它不自动启动 ResearchRun，不调用 CloudOpportunityAssessmentProvider，也不创建 Assessment、Signal、ProductHypothesis、MarketEvidence 或推荐。

调度器位于 `app/main.py` 的 FastAPI lifespan 中，由 `ENABLE_SCHEDULER` 控制；显式 CLI 命令 `app.cli collect` 和 `POST /api/run` 使用同一个 Pipeline 实现。`app.cli run` 只启动 Web 服务，不创建 Pipeline 运行。

## 3. Web 与 API

Web 层由 `app/main.py` 和 Jinja 模板组成。默认导航只有“发现趋势”“判断任务”“判断记录”，对应 `/`、`/workbench` 和 `/workbench/processed`。判断详情固定按四步进度、唯一下一步、AI 三级判断卡、证据、人工确认和折叠技术审计组织。事件详情只展示趋势事实、证据和初筛状态。

`/research`、`/signals`、`/feedback`、`/hypotheses`、`/validation`、`/recommendations` 和 `/semantic-review` 等旧页面仍保留原 URL 和 API，供直接 URL 回滚，但默认导航、Dashboard、事件详情和工作台不再提供入口或交叉链接。

API 分为以下边界：

- Pipeline 和运行状态 API；
- Event、Evidence、EvidenceBundle 和初筛复核 API；
- ResearchCandidate、ResearchRun 和受控工具 API；
- 人工/云端 OpportunityAssessment 及审核 API；
- OpportunitySignal 反馈 API；
- ProductHypothesis、MarketEvidence 和 ValidatedRecommendation API；
- 迁移期旧机会、旧验证和推送兼容 API。

写 API通过中间件执行本机/Origin 检查；配置 `ADMIN_TOKEN` 时要求 `X-Admin-Token` 请求头。该机制只适合本机或可信内网，不是正式身份认证、角色权限或公网限流方案。

`/workbench`、`/workbench/processed` 和 `/workbench/items/{candidate_id}` 是当前用户流程页面。工作台通过 `POST /api/workbench/research-candidates/{id}/ai-draft` 薄编排现有 ResearchRun 与 `CloudOpportunityAssessmentProvider`，只创建 pending Assessment；审核使用独立 Assessment review API。`POST /api/workbench/research-candidates/{id}/evidence` 在一个 SQLite 事务内保存人工 Evidence、重建 Bundle、创建继任 Candidate 并登记替代关系。

## 4. 数据库存储

SQLite 是业务事实和状态机的唯一持久化来源。主要对象组为：

- 运行与来源：`pipeline_runs`、`source_snapshots`、`source_items`、`job_leases`。
- 事件与聚类：`trend_events`、`event_members`。
- 初筛与证据：`research_screenings`、`research_screening_reviews`、`evidence_collection_runs`、`evidence`、`evidence_bundles`。
- 可选语义：`semantic_event_features`、`semantic_duplicate_candidates` 及反馈/评测标签。
- 研究与判断：`research_candidates`、`research_runs`、`research_tool_calls`、`opportunity_assessments`。
- 新业务链：`opportunity_signals`、`product_hypotheses`、`market_evidence`、`validated_recommendations` 及对应反馈。
- 旧兼容链：`analyses`、`product_opportunities`、`market_validations`、`opportunity_outcomes` 和历史推送表。

EvidenceBundle 按 `event_id + input_hash + version` 保存不可变快照。新的证据集合产生新 Bundle；ResearchCandidate 指向用于其判断的具体 Bundle。补证不会改写旧 Candidate 或 Assessment，而是由新 Bundle 创建继任 Candidate，旧版本可在判断详情的折叠历史中回看。

ResearchRun 保存执行者、预算和生命周期。受控工具调用只保存工具名、请求哈希、状态、耗时、结果 Evidence ID 和脱敏错误，不保存密钥、Cookie 或登录页正文。

OpportunityAssessment v2 保存消费变化、新问题、研究建议三个带依据和 Evidence ID 的 JSON 判断，以及现有解决方式、解决缺口、事实/推断引用、缺失证据、生成状态、Provider/模型/版本、结构化人工审核详情和审核状态。引用必须属于同一 Event 且包含在 Candidate 的 Bundle 中。历史 v1 行按原字段只读解码，不补造 v2 判断。

## 5. 核心技术对象关系

```text
TrendEvent
  ├─ Evidence[]
  └─ EvidenceBundle (immutable snapshot)
       └─ ResearchCandidate
            ├─ ResearchRun
            │    └─ ResearchToolCall[]
            └─ OpportunityAssessment v2
                  ├─ needs_more_evidence → 新 EvidenceBundle / 继任 Candidate
                  ├─ rejected → 已审核判断卡（放弃）
                  └─ approved worth_following → 已审核判断卡（继续研究）
                       └─ OpportunitySignal（内部兼容对象，默认 UI 隐藏）
```

- **Evidence** 是单条公开或人工证据，保存内容强度、来源、获取状态、摘录和哈希。
- **EvidenceBundle** 是某一时点的证据准备度快照，不是研究结论。
- **OpportunityAssessment** 是基于 Bundle 的三级结构化判断草稿；它可以弃权、标记证据不足或因技术失败保持不可审核。
- **已审核判断卡** 是当前用户侧核心业务产出，由人工三项确认和最终动作构成。
- **OpportunitySignal** 只由已批准且 `worth_following` 的 Assessment 创建，当前仅为内部状态机兼容对象。
- **ProductHypothesis** 是从 Signal 派生的具体实体商品方向，仍未经过市场验证。
- **MarketEvidence** 是平台或人工验证证据；只有完整且通过风险、经济性门槛时才形成 ValidatedRecommendation。

OpportunitySignal 的 `analysis_id` 当前仍为非空外键。批准 Assessment 时，应用会额外创建兼容用 `pipeline_runs` 和 `analyses` 记录，再写入 Signal。这是旧 Schema 约束，不表示旧 Analyzer 仍在运行。

## 6. 当前旧链兼容结构

仓库仍保留旧对象和接口：

- `analyses` 保存历史分析以及新 Signal 所需的兼容审计行；
- `product_opportunities` 和 `market_validations` 保存历史规则产物及验证；
- 旧机会审核、验证、查询词、结果回流和推送 API 仍存在；
- 旧下游页面只能通过直接 URL 访问，默认页面不展示商品机会或交叉链接；
- `PRODUCT_HYPOTHESIS_WORKBENCH_ENABLED=false` 阻止旧商品机会生成活跃验证队列；
- 直接创建 OpportunitySignal 的旧 API 固定返回 HTTP 410。

旧链不是当前自动 Pipeline 的下游，也不能进入新 `validated_recommendations`。长期移除旧链需要单独的数据迁移和 API 退役任务，不能作为小功能附带完成。

## 7. 当前冻结能力

以下能力当前明确冻结或未接入：

- 自动语义合并 Event；语义相似只生成待人工复核候选。
- 自动 Research Agent、独立 Agent worker 和自动 Candidate 调度。
- 浏览器渲染、浏览器登录态、Cookie 持久化和验证码处理。
- Pipeline 自动调用大模型或自动生成 Assessment。
- 模型自动审核 Assessment、创建 OpportunitySignal、生成 ProductHypothesis 或推荐。
- 旧 Analyzer、固定商品模板和 `local-rules` 商品生成回退。
- 用假设分、向量相似度或模型置信度填补缺失市场证据。
- 默认测试或启动时联网下载 Embedding 模型。

`CloudOpportunityAssessmentProvider` 不属于冻结能力：v2 Provider、工作台入口、审核与补证闭环均已接入。完整自主 Research Agent 仍未实现，不能因存在 Skill、ResearchRun 或受控工具而描述为已接入。
