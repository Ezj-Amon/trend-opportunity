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

调度器位于 `app/main.py` 的 FastAPI lifespan 中，由 `ENABLE_SCHEDULER` 控制；CLI 和 `POST /api/run` 使用同一个 Pipeline 实例。

## 3. Web 与 API

Web 层由 `app/main.py` 和 Jinja 模板组成。主页面按用户任务组织：趋势发现、机会判断、商品方向、市场验证和已验证选品；技术审计信息在事件页或系统入口中折叠显示。

API 分为以下边界：

- Pipeline 和运行状态 API；
- Event、Evidence、EvidenceBundle 和初筛复核 API；
- ResearchCandidate、ResearchRun 和受控工具 API；
- 人工/云端 OpportunityAssessment 及审核 API；
- OpportunitySignal 反馈 API；
- ProductHypothesis、MarketEvidence 和 ValidatedRecommendation API；
- 迁移期旧机会、旧验证和推送兼容 API。

写 API通过中间件执行本机/Origin 检查；配置 `ADMIN_TOKEN` 时要求 Bearer Token。该机制只适合本机或可信内网，不是正式身份认证、角色权限或公网限流方案。

当前事件页面主要使用人工结构化 OpportunityAssessment 表单。`POST /api/research-candidates/{id}/assessments/cloud` 已存在，但模板和 JavaScript 尚未调用它。

## 4. 数据库存储

SQLite 是业务事实和状态机的唯一持久化来源。主要对象组为：

- 运行与来源：`pipeline_runs`、`source_snapshots`、`source_items`、`job_leases`。
- 事件与聚类：`trend_events`、`event_members`。
- 初筛与证据：`research_screenings`、`research_screening_reviews`、`evidence_collection_runs`、`evidence`、`evidence_bundles`。
- 可选语义：`semantic_event_features`、`semantic_duplicate_candidates` 及反馈/评测标签。
- 研究与判断：`research_candidates`、`research_runs`、`research_tool_calls`、`opportunity_assessments`。
- 新业务链：`opportunity_signals`、`product_hypotheses`、`market_evidence`、`validated_recommendations` 及对应反馈。
- 旧兼容链：`analyses`、`product_opportunities`、`market_validations`、`opportunity_outcomes` 和历史推送表。

EvidenceBundle 按 `event_id + input_hash + version` 保存不可变快照。新的证据集合产生新 Bundle；ResearchCandidate 指向用于其判断的具体 Bundle。

ResearchRun 保存执行者、预算和生命周期。受控工具调用只保存工具名、请求哈希、状态、耗时、结果 Evidence ID 和脱敏错误，不保存密钥、Cookie 或登录页正文。

OpportunityAssessment 保存结构化判断、事实/推断引用、缺失证据、Provider/模型/版本及审核状态。引用必须属于同一 Event 且包含在 Candidate 的 Bundle 中。

## 5. 核心技术对象关系

```text
TrendEvent
  ├─ Evidence[]
  └─ EvidenceBundle (immutable snapshot)
       └─ ResearchCandidate
            ├─ ResearchRun
            │    └─ ResearchToolCall[]
            └─ OpportunityAssessment
                  └─ approved worth_following
                       └─ OpportunitySignal
                            └─ ProductHypothesis
                                 └─ MarketEvidence[]
                                      └─ ValidatedRecommendation
```

- **Evidence** 是单条公开或人工证据，保存内容强度、来源、获取状态、摘录和哈希。
- **EvidenceBundle** 是某一时点的证据准备度快照，不是研究结论。
- **OpportunityAssessment** 是基于 Bundle 的结构化判断草稿或人工判断；它可以弃权或标记证据不足。
- **OpportunitySignal** 只由已批准且 `worth_following` 的 Assessment 创建，是第一阶段核心业务产出。
- **ProductHypothesis** 是从 Signal 派生的具体实体商品方向，仍未经过市场验证。
- **MarketEvidence** 是平台或人工验证证据；只有完整且通过风险、经济性门槛时才形成 ValidatedRecommendation。

OpportunitySignal 的 `analysis_id` 当前仍为非空外键。批准 Assessment 时，应用会额外创建兼容用 `pipeline_runs` 和 `analyses` 记录，再写入 Signal。这是旧 Schema 约束，不表示旧 Analyzer 仍在运行。

## 6. 当前旧链兼容结构

仓库仍保留旧对象和接口：

- `analyses` 保存历史分析以及新 Signal 所需的兼容审计行；
- `product_opportunities` 和 `market_validations` 保存历史规则产物及验证；
- 旧机会审核、验证、查询词、结果回流和推送 API 仍存在；
- 事件页在折叠审计区域只读展示旧商品机会；
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

`CloudOpportunityAssessmentProvider` 不属于冻结能力：它的后端实现和 API 已存在，当前缺口是事件页面接入。完整自主 Research Agent 仍未实现，不能因存在 Skill、ResearchRun 或受控工具而描述为已接入。
