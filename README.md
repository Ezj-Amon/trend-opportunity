# 趋势机会判断训练器

> 项目状态：已于 2026-07-23 封存，不再继续开发。仓库保留为本次探索的最终实现与经验记录。

这是一个帮助不具备专业选品经验的人，结构化完成趋势机会判断的个人研究项目。系统采集公开趋势、合并重复事件、执行规则初筛和有限证据采集，再由 AI 把证据整理成可核对的三级判断卡。

当前用户侧闭环截止到“已审核判断卡”：确认事实是否准确、问题是否真实、持续性是否足够，然后选择继续研究、补充证据或放弃。系统不要求形成商品方向、Amazon 市场验证或推荐。

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

后续由页面中的显式动作推进：

```text
ResearchCandidate
→ ResearchRun
→ AI 三级 OpportunityAssessment 判断卡
→ 人工确认事实、问题和持续性
→ 继续研究 / 补充证据 / 放弃
→ 已审核判断卡
```

默认 UI 只有三个入口：`/` 发现趋势、`/workbench` 判断任务、`/workbench/processed` 判断记录。用户选择“继续研究”时，后端仍创建内部 `OpportunitySignal` 以兼容既有状态机，但默认 UI 不展示 Signal 或任何下游入口。旧下游页面和 API 暂时保留，只有直接 URL 可访问，便于回滚。

## 目标流程

在 EvidenceBundle 就绪后，模型生成带证据引用的 OpportunityAssessment v2 草稿，依次回答“是否为消费变化、是否产生新问题、是否值得继续研究”。人工只需核对三个明确问题并作出决定；已审核判断卡是当前用户侧交付物。

没有合格事件、证据或判断时，合法结果可以为空。商品方向和市场验证代码仍作为兼容能力保留，但不属于当前产品流程。

详细节点合同见 [业务流程合同](docs/workflow-contract.md)。

## 大模型使用边界

`CloudOpportunityAssessmentProvider` 通过 OpenAI Structured Outputs 基于既有 EvidenceBundle 生成结构化 Assessment v2。它必须输出三级判断及依据，区分事实与推断，并明确现有解决方式、解决缺口和缺失证据；不得输出商品名、商品类目、平台查询词、价格或推荐。

当前自动 Pipeline 不调用大模型。判断任务通过显式按钮调用 Cloud Provider，并只保存待审核草稿；没有云端模型时页面会给出配置和重启指引。模型调用失败会保存技术审计并允许重试，不会伪装成一张可审核的“弃权判断卡”。

## 当前实现状态

- 已实现：真实趋势采集、标题标准化、词面聚类、受保护的重复合并、趋势评分与 Top-N、规则初筛、有限公开证据采集、EvidenceBundle、ResearchCandidate 和 ResearchRun。
- 已实现：OpportunityAssessment v2 三级判断、引用校验、技术失败重试、结构化人工审核、判断记录和内部 Signal 兼容门。
- 已实现：人工补证后原子创建新 Evidence、不可变 Bundle 和继任 Candidate；旧判断与决定不可改写。
- 默认隐藏：ProductHypothesis、Seller Central CSV/人工 MarketEvidence、ValidatedRecommendation 及其他下游页面入口。
- 尚未实现：完整自主 Research Agent、浏览器证据执行器和独立 Agent worker。
- 当前冻结：自动语义合并、浏览器登录态、旧 Analyzer/固定商品模板，以及任何自动生成 Signal、商品或推荐的路径。
- 兼容保留：旧 `analyses`、`product_opportunities`、`market_validations` 及部分旧 API；它们不是新主链的生产者。

## 快速启动

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)：

```powershell
uv sync --extra dev
uv run python -m app.cli run
```

访问 <http://127.0.0.1:8000>，从“发现趋势”开始；判断任务位于 <http://127.0.0.1:8000/workbench>。配置示例见 `.env.example`；应用不会自动加载 `.env`。

`app.cli run` 只启动 Web 服务，不会立即执行采集。需要手工采集时，可在首页点击“运行真实采集”，或显式执行：

```powershell
uv run python -m app.cli collect
```

也可以不经过 CLI，直接启动同一个 Web 应用：

```powershell
uv run python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

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
