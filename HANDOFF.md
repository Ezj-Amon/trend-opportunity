# 当前开发交接

更新时间：2026-07-22

当前工作分支：`docs/workflow-governance`

当前 main 基线：`3cd94b3`（本轮开始时与 `origin/main` 一致）

本文件只记录当前开发状态和下一项任务。业务流程以 [docs/workflow-contract.md](docs/workflow-contract.md) 为准，技术结构以 [docs/architecture.md](docs/architecture.md) 为准。

## 当前真实实现

- 自动 Pipeline 已实现趋势采集、标题标准化、词面聚类、受保护重复合并、趋势评分、国内/海外 Top-N、规则初筛、有限公开证据采集、EvidenceBundle 和 ResearchCandidate。
- 自动 Pipeline 在 ResearchCandidate 处结束，不自动启动 ResearchRun，不调用大模型，也不创建 Assessment、Signal、商品方向或推荐。
- EvidenceBundle ready 的硬门槛是至少两个独立有效来源，并且至少一篇完整正文或官方公告；质量分只作诊断，不依赖 `EVIDENCE_READY_SCORE`。
- ResearchRun、受控研究工具、HumanAssessmentProvider、CloudOpportunityAssessmentProvider、引用校验、人工审核和 OpportunitySignal 状态门均已实现。
- 当前事件页面仍以人工填写 OpportunityAssessment 为主。人工快捷接口会建立 Human ResearchRun、保存 Assessment，并按人工选择完成审核。
- ProductHypothesis、Seller Central CSV/人工 MarketEvidence、风险和经济性门、ValidatedRecommendation 已有独立新链。
- 旧 `analyses`、`product_opportunities`、`market_validations` 和部分兼容 API仍保留，但不是当前自动 Pipeline 的下游生产者。
- 自动语义合并、浏览器登录态、旧 Analyzer/固定商品模板和自动创建 Signal/商品/推荐均保持冻结。

## 当前缺口

- CloudOpportunityAssessmentProvider 尚未接入事件页面；当前只能通过显式 API 创建云端 Assessment 草稿。
- 页面主路径尚未清晰分离“AI 生成草稿”和“人工独立审核”。
- 尚未实现完整自主 Research Agent、独立 Agent worker、自动 Candidate 调度或浏览器证据执行器。
- 写 API仍是本机/可信内网级保护，没有正式身份、角色和公网限流。
- 新 Signal 仍受旧 `opportunity_signals.analysis_id` 非空外键约束，批准 Assessment 时需要写一条兼容 `analyses` 记录。
- 旧链表和 API 的正式退役需要独立迁移任务，不应夹带在小功能中完成。

## 本轮文档整理

- 重写 `README.md`，只保留项目入口、真实/目标流程概览、模型边界、状态、启动和导航。
- 新增根目录 `AGENTS.md`，规定文档职责、权威顺序和变更纪律。
- 新增 `docs/workflow-contract.md`，作为业务节点、停止条件和 AI 边界的唯一真相来源。
- 将产品边界与架构内容收敛为 `docs/architecture.md`，作为技术结构的唯一真相来源。
- 将旧 Research 架构、实施计划和海外来源调研移入 `docs/archive/`，并标记为 Historical / Archived。
- 保留并核对 `docs/amazon-first-party-validation.md`。
- 保留 Research Skill，只修正 EvidenceBundle 门槛、模型权限和自主 Agent 状态。
- 删除旧 HANDOFF 中容易失效的数据库数量、测试数量、历史工作树和未合并状态。

## 下一项开发任务

下一项开发任务是：将 `CloudOpportunityAssessmentProvider` 接入事件页面。

该任务应让用户对满足前置条件的 ResearchCandidate 显式生成 AI OpportunityAssessment 草稿，展示调用中、成功、弃权和失败状态，再由人工独立批准、驳回或要求更多证据。不得让模型自动批准、直接创建 OpportunitySignal 或生成商品。

完整自主 Research Agent 尚未实现，不属于上述页面接入已经具备的能力。
