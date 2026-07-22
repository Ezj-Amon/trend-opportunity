# 全球趋势驱动新品机会系统

这是一个从国内外公开趋势中识别实体消费变化的个人研究项目。系统采集趋势信号、合并重复事件、执行规则初筛和有限证据采集，再把证据充分的事件交给结构化机会判断。

第一阶段的核心闭环截止到：人工批准 OpportunityAssessment 后形成 OpportunitySignal。商品方向、Amazon 市场验证和已验证推荐属于后续验证链。

## 项目不是什么

- 不是新闻转载站，也不把热点热度等同于购买需求。
- 不是每日商品生成器；系统不要求每天产出商品、机会或推荐。
- 不是自动选品黑箱；任意节点都允许因风险、证据不足、相关性弱或没有合格结果而停止。
- 不用模型置信度、向量相似度或假设分替代平台市场证据。
- 不绕过登录、验证码、付费墙或站点访问控制。

## 当前真实流程

当前自动 Pipeline 的实际范围是：

```text
定时或手工触发
→ 趋势采集
→ 标准化、聚类和去重
→ 趋势评分与国内/海外 Top-N 选择
→ 规则初筛
→ 最小证据采集
→ EvidenceBundle 就绪判断
→ ResearchCandidate
→ 自动 Pipeline 结束
```

后续由页面或显式 API 推进：

```text
ResearchCandidate
→ ResearchRun
→ OpportunityAssessment
→ 人工审核
→ OpportunitySignal
→ ProductHypothesis
→ MarketEvidence
→ ValidatedRecommendation
```

当前事件页面仍以人工填写 OpportunityAssessment 为主。页面的“完成机会判断”会建立 Human ResearchRun、保存人工 Assessment，并按人工选择完成审核。系统不会自动创建 OpportunitySignal。

## 目标流程

目标是在 EvidenceBundle 就绪后，由模型生成带证据引用的 OpportunityAssessment 草稿，再由人工独立审核；只有批准的 `worth_following` Assessment 才形成 OpportunitySignal。第一阶段以这个人工批准后的 Signal 为交付边界。

商品方向和市场验证继续遵循独立证据链。没有合格事件、证据、判断或市场验证时，合法结果可以为空。

详细节点合同见 [业务流程合同](docs/workflow-contract.md)。

## 大模型使用边界

`CloudOpportunityAssessmentProvider` 已存在，并通过 OpenAI Structured Outputs 基于既有 EvidenceBundle 生成结构化 Assessment。它不得补写无法访问的事实，不得自动批准 Assessment，不得直接生成 OpportunitySignal、ProductHypothesis 或推荐。

当前自动 Pipeline 不调用大模型，事件页面也尚未接入 Cloud Provider。云端 Assessment 目前只能通过显式 API 创建；没有云端模型时，人工流程仍可工作。可选 Embedding 只用于检索、类目联想和重复候选，不承担最终机会判断。

## 当前实现状态

- 已实现：真实趋势采集、标题标准化、词面聚类、受保护的重复合并、趋势评分与 Top-N、规则初筛、有限公开证据采集、EvidenceBundle、ResearchCandidate、ResearchRun、人工/云端 Assessment Provider、引用校验、人工审核和 Signal 状态门。
- 已实现后续能力：人工 ProductHypothesis、Seller Central CSV/人工 MarketEvidence、风险与经济性门、ValidatedRecommendation。
- 尚未接入：事件页面上的云端 Assessment 草稿生成。
- 尚未实现：完整自主 Research Agent、浏览器证据执行器和独立 Agent worker。
- 当前冻结：自动语义合并、浏览器登录态、旧 Analyzer/固定商品模板，以及任何自动生成 Signal、商品或推荐的路径。
- 兼容保留：旧 `analyses`、`product_opportunities`、`market_validations` 及部分旧 API；它们不是新主链的生产者。

## 快速启动

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)：

```powershell
uv sync --extra dev
uv run python -m app.cli run
uv run python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

访问 <http://127.0.0.1:8000>。配置示例见 `.env.example`；应用不会自动加载 `.env`。

核心测试：

```powershell
uv run pytest -q tests\test_core.py
```

真实外部来源测试会访问上游服务：

```powershell
uv run pytest -q tests\test_live_sources.py
```

## 文档导航

- [业务流程合同](docs/workflow-contract.md)：业务节点、停止条件、AI 边界、当前实现与目标差距的唯一真相来源。
- [技术架构](docs/architecture.md)：模块关系、运行结构、数据对象关系、兼容结构和冻结能力的唯一真相来源。
- [Amazon 一方数据验证](docs/amazon-first-party-validation.md)：ProductHypothesis 到 MarketEvidence 的专项操作说明。
- [开发交接](HANDOFF.md)：当前开发状态、缺口和下一项任务。
- [研究 Skill](skills/trend-opportunity-research/SKILL.md)：运行时证据研究和弃权规则。
- [历史设计](docs/archive/)：仅供追溯，不是当前实现依据。
- [文档与开发治理](AGENTS.md)：文档职责、权威顺序和变更纪律。
