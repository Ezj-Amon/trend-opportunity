# 全球趋势驱动新品机会系统

一个使用真实热点数据的个人验证项目：采集国内外热榜、搜索和社区信号，聚类成趋势事件，计算趋势分并保存来源证据，再逐步识别值得跟进的新品机会线索。

## 产品方向

本项目的核心定位是：从全球新闻趋势中发现可能孕育新品的消费变化，并将其整理成可验证的新品机会线索。趋势事件、机会线索、具体商品假设、平台市场证据和已验证推荐必须分层处理；没有平台证据时不得把商品假设包装成最终选品推荐。

Web 主导航按用户任务收敛为“趋势发现 → 机会判断 → 商品方向 → 市场验证 → 已验证选品”。语义评测、数据源状态和审计记录位于“系统”二级入口；事件页优先展示当前结论、1–2 条关键证据和唯一下一步，未采用来源及模型、运行、工具细节默认折叠。

固定产品模板、假设分代理市场分和未验证产品榜单已经在 Phase 0 停用。后续设计与开发以 [产品边界与分层架构](docs/product-boundary-and-architecture.md) 为准。

## 目标研究架构

系统已采用“核心程序 + Evidence 工具 + Research Skill + Research Agent”的混合架构，不把系统改造成纯 Agent：

```text
趋势事件（发现正在发生什么）
  -> 证据包（区分正文、标题、消费者声音和抓取失败）
  -> 待研究候选（保存值得调查的方向，不等于机会结论）
  -> Research Agent + Skill（搜索、抓取、核对和补充证据）
  -> 机会评估（事实、推断、引用、缺失证据和弃权原因）
  -> 人工确认
  -> 新品机会线索 -> 商品假设 -> 市场证据 -> 已验证推荐
```

- 核心程序继续负责数据库、状态机、审计、风险门和定时运行，是唯一事实来源。
- Evidence 工具负责公开网页、来源适配和人工证据接入；MCP 只是可选工具接口。浏览器证据和独立自动 Agent 尚未接入执行器，也没有可误启用的环境开关。
- Skill 固化研究步骤与证据标准，Agent 负责编排工具，大模型只对已有证据做带引用的综合。
- Embedding 继续用于检索、跨语言匹配、重复候选和类目联想，不承担最终机会判断。

该研究链的核心程序结构和阶段 0–7 接口已实现：包含不可变 `EvidenceBundle`、`ResearchCandidate`、可恢复 `ResearchRun`、受控研究工具、人工/可选云端 `OpportunityAssessment`、引用校验和人工审核。Pipeline 会先执行可审计的快速初筛，明显的灾难、赛事、人物、软件/代码和医疗功效主题在正文深抓前退出；仅凭标题无法确认实体消费关联的事件进入 `/research` 的“初筛待复核”，由用户一次性选择排除或允许一次有限补证。自动通过和人工允许的补证都只搜索 1 次、尝试最多 4 个公开页面，并在取得 2 个独立来源且至少 1 篇完整正文或官方公告后立即停止。默认 `ENABLE_EMBEDDINGS=false` 时，Pipeline 仍可为至少达到 `partial` 的安全 Bundle 创建无类目待判断趋势；纯标题 `insufficient` Bundle 继续弃权。MCP 与浏览器登录态不是核心依赖，浏览器证据和独立自动 Agent 尚未接入；系统不会自动创建 OpportunitySignal。详细决策见 [研究架构](docs/research-agent-architecture.md)，表结构、接口与验收标准见 [实施计划](docs/research-agent-implementation-plan.md)，当前状态见 [HANDOFF](HANDOFF.md)。

## 当前能力

- 国内数据：默认通过 NewsNow 公共 API 采集微博、知乎、百度、抖音、头条、B 站、酷安和贴吧。
- 海外数据：默认采集 US/GB/DE/JP Google Trends RSS，以及 Hacker News、Product Hunt、GitHub Trending；后台可按市场筛选。
- 海外社媒：提供 Reddit OAuth 可选适配器，使用 Async PRAW 管理鉴权、分页和限流，不依赖不稳定的匿名抓取。
- Amazon 验证能力：已将 Seller Central CSV 解析封装为 `MarketplaceDataProvider`，证据写入独立 `market_evidence`，不再依赖旧机会表。
- 可追溯：保存每次请求的状态、延迟、原始响应哈希、热榜排名和事件聚类关系。
- 可解释：趋势分由代码计算并显示所有分项；模型判断分、市场分和已验证推荐分分别保存。
- 证据优先：热榜/搜索落地页只作为信号，不再伪装成正文；正文使用 Trafilatura 抽取并经过长度、模板污染和事件相关性校验。
- 独立来源补证：默认通过无需密钥的 Google News RSS 主动查找原站报道并解码直链；可选接入自建 SearXNG。独立来源按注册域名计数，转载近重复内容不重复计数。
- 抓取前初筛：先检查实体消费关联、持续性、交付周期和阻断风险；初筛决定和原因会单独保存，未通过事件不消耗正文抓取预算。
- 初筛人工复核：消费关联不明确的 `needs_review` 事件会进入机会判断页；复核决定不可改写，排除不抓取，允许补证只执行一个有预算且可提前停止的采集记录。
- 证据够用即停：逐页保存并重算 EvidenceBundle；达到 2 个独立来源且至少 1 篇完整正文或官方公告后立即停止，第二来源允许是通过质量校验的可靠摘要。
- 研究分层：定时 Pipeline 只构建 EvidenceBundle 和 ResearchCandidate；可选云端模型只能生成带引用的 OpportunityAssessment，不能直接生成 Signal 或商品假设。
- 页面机会判断：事件页可直接选择“值得跟进 / 不适合选品 / 需要补证据”，勾选已采用证据并提交结构化判断；一次操作沿受控状态链完成 ResearchRun、OpportunityAssessment 和人工审核，只有“值得跟进”且证据就绪时才创建已确认机会。
- 安全门：死亡、犯罪和受害者相关敏感事件不生成商业化建议。
- 主动弃权：证据不足、敏感事件或模型失败会形成显式弃权状态，不使用商品模板或本地规则伪装分析结果。
- 推送门槛：单条商品只有完成人工审核、市场验证和风险门后才能作为已验证推荐推送，重复推送具有幂等保护。
- 趋势摘要：首页和飞书分别展示中国、海外事实层趋势信号 Top 3，并明确标注它们不是商品推荐。
- 市场验证分层：假设分、Amazon 一方市场分和可空的已验证推荐分分开保存；未完成市场验证时推荐分为空，不补写搜索量、竞争或利润数据。
- 产品风险门：合规、IP、物流、季节性、供应链和单位经济性风险结构化记录，阻断级机会不能进入榜单、通过审核或推送。
- 反馈闭环：支持保存审核原因、7 天结果和 30 天结果，为后续复盘提供标签。
- OpportunitySignal 主链路：趋势事件与商品假设之间已有独立机会线索表，保存变化类型、消费关联、目标用户、新场景、未满足需求、实体类目、耐久性、交付周期、证据引用、缺失证据以及引擎/模型/版本。
- 线索反馈：兼容页 `/signals` 仍可查看新品机会线索，系统二级入口中的 `/feedback` 提供“值得跟进、无实体商品机会、消费关联弱、过于短期、类目错误、证据不足”六类反馈；每次反馈保存当时的趋势、证据、线索文本和模型版本快照。
- 可选语义基线：以 `intfloat/multilingual-e5-small` 为默认模型，用事件标题和短证据摘要生成向量，保存模型 ID、revision、输入哈希和特征版本；正负机会原型相似度与实体类目候选只用于发现和排序，不显示为需求概率。
- 语义去重闭环：`/semantic-review` 展示语义重复候选和真实事件评测样本；候选保存模型/特征版本、输入哈希、词面相似度、市场和语言，人工反馈保留完整快照，永不自动合并事件。
- 主流程不再提供绕过机会判断的人工 Signal 创建按钮；直接创建 Signal 的兼容 API 固定返回 410，所有新机会都必须来自证据就绪且已批准的 OpportunityAssessment。
- 独立商品方向：事件页提供结构化表单并自动继承已确认机会的证据，`/hypotheses` 负责审核；只有完整受控链上的实体方向才能进入验证，每个机会最多保留 3 个有效方向。
- 独立推荐链：`/validation` 写入 MarketEvidence；只有证据完整、单位经济和证据评分至少为 3、风险为低或中时，才在 `/recommendations` 生成可回溯的 ValidatedRecommendation。

## 快速启动

先同步项目虚拟环境（不会安装到系统 Python）：

```powershell
uv sync --extra dev
uv run python -m app.cli run
# 或直接启动 Web 服务：
uv run python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

然后访问 <http://127.0.0.1:8000>。

聚类规则升级或本地派生数据需要重建时，可执行 `python -m app.cli rebuild`。该命令保留原始来源快照与条目，清空事件、证据、分析、机会和推送记录，再进行一次新的真实采集；运行前应先备份需要保留的审核结果。

如果不使用 uv，必须先激活自己的虚拟环境，再安装项目；直接使用系统 `python` 而未安装依赖会出现 `ModuleNotFoundError`：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

所有配置均为环境变量，示例见 `.env.example`。应用不会自动读取 `.env`，避免无意加载密钥；PowerShell 中可用 `$env:变量名='值'` 设置。

默认会为研究候选主动搜索公开新闻，无需 API Key：

```powershell
$env:ENABLE_PUBLIC_NEWS_SEARCH='true'
$env:PUBLIC_NEWS_MAX_RESULTS='4'
# 可选：只填写自己控制或信任的 SearXNG 实例
$env:SEARXNG_BASE_URL='https://search.example.com'
```

搜索只提供候选 URL；原站页面仍必须通过公网地址、正文真实性和相关性校验。系统不会绕过登录、验证码或付费墙。

## 海外源与 Reddit

默认海外发现层无需账户：

```powershell
$env:GOOGLE_TRENDS_GEOS='US,GB,DE,JP'
$env:OVERSEAS_RESEARCH_CANDIDATE_TOP_N='5'
uv run python -m app.cli run
```

需要加入 Reddit 消费讨论时，请创建 Reddit API 应用并配置 OAuth 凭证：

```powershell
$env:REDDIT_CLIENT_ID='...'
$env:REDDIT_CLIENT_SECRET='...'
$env:REDDIT_USER_AGENT='TrendOpportunityLab/0.2 by your-reddit-name'
$env:REDDIT_SUBREDDITS='BuyItForLife,gadgets,HomeImprovement,shutupandtakemymoney'
uv run python -m app.cli run
```

未配置凭证时不会请求 Reddit，也不会把 Reddit 标成失败源。完整的成熟项目、核心代码、许可证、Issue 和实测比较见 [海外数据源调研](docs/overseas-source-research.md)。

默认情况下，管理写接口只接受本机请求，并校验浏览器 `Origin`。如果需要从可信内网访问，必须设置 `ADMIN_TOKEN`，并在浏览器控制台执行 `localStorage.setItem('trendAdminToken', '同一个令牌')`。这只是 Demo 级保护；公网部署仍应接入正式身份认证和限流。

## 使用真实模型

```powershell
$env:OPENAI_API_KEY='你的密钥'
$env:OPENAI_MODEL='gpt-4.1-mini'
# 使用兼容服务时再设置：
$env:OPENAI_BASE_URL='https://example.com/v1'
uv run python -m app.cli run
```

模型输出必须通过 Pydantic Schema，并且只能引用数据库中存在且属于当前 Bundle 的证据 ID。如果模型失败，OpportunityAssessment 会明确记录 `abstained` 和失败原因，不会回退到本地商品规则。

## 飞书推送

创建飞书群自定义机器人后设置：

```powershell
$env:FEISHU_WEBHOOK_URL='https://open.feishu.cn/open-apis/bot/v2/hook/...'
$env:FEISHU_SECRET='机器人签名密钥（未开启签名可不填）'
$env:PUBLIC_BASE_URL='http://你的可访问地址:8000'
```

单条商品必须完成市场验证并通过人工审核，才能点击“推送已验证推荐”。Webhook 和 Secret 只从环境变量读取，不写入 SQLite。

首页还提供“推送趋势摘要到飞书”：一次发送中国与海外趋势信号 Top 3。摘要只包含事实层趋势事件，不包含商品、查询词或选品推荐。

## 商品假设、市场验证与推荐

先在事件页完成机会判断；“值得跟进”且证据就绪时会直接形成已确认机会，无需再到隐藏反馈页二次批准。随后在同一事件页填写结构化实体商品方向，系统自动引用上游证据；商品方向通过结构和风险检查并经审核后进入 `/validation`。完整操作见 [Amazon 一方数据验证流程](docs/amazon-first-party-validation.md)。

系统不再从新闻关键词或规则类目自动生成 Amazon 查询词。查询词必须来自具体商品形态、目标用户和使用场景，并通过人工确认；修正查询词后旧市场验证自动失效。

程序明确区分事件的信号来源市场和产品的目标 Amazon 站点。默认目标站点由 `AMAZON_DEFAULT_MARKETPLACE` 控制；更换目标站点会让旧站点验证失效并把审核状态重置为待审核。

新主链路使用可插拔 `MarketplaceDataProvider` 契约和以下 API：

```text
POST /api/research-screenings/{screening_id}/review
POST /api/research-candidates/{candidate_id}/opportunity-judgment
POST /api/opportunity-signals/{signal_id}/product-hypotheses
POST /api/product-hypotheses/{hypothesis_id}/review
POST /api/product-hypotheses/{hypothesis_id}/amazon-raw-import
POST /api/product-hypotheses/{hypothesis_id}/market-evidence
GET  /api/validated-recommendations
```

旧 `product_opportunities`、`market_validations` 及对应 API 仅作迁移兼容，不再生产新主链路推荐。八个市场维度依次为搜索需求、购买意图、竞争机会、单位经济性、差异化、执行可行性、时机持续性和证据完整度，未知项必须留空；任何缺失都会阻止最终推荐。

审核时可填写原因，机会详情页还可记录 7 天和 30 天结果。写接口仍遵守本机或 `ADMIN_TOKEN` 限制。

## 测试

```powershell
uv run pytest -q
uv run pytest -q -m live
```

`live` 测试会真实访问 NewsNow 和 Google Trends RSS，不使用 Mock。由于它依赖外部服务，网络或上游故障会真实导致测试失败。Reddit 需要个人 OAuth 凭证，因此不放入无凭证 CI 测试。

## 可选语义模型

默认安装不包含 PyTorch 或 `sentence-transformers`，默认测试不会下载模型。需要启用时先显式安装并准备本地缓存：

```powershell
uv sync --extra dev --extra ml
$env:ENABLE_EMBEDDINGS='true'
$env:EMBEDDING_MODEL_ID='intfloat/multilingual-e5-small'
$env:EMBEDDING_MODEL_REVISION='614241f622f53c4eeff9890bdc4f31cfecc418b3'
$env:EMBEDDING_CACHE_DIR='data/models'
$env:EMBEDDING_LOCAL_FILES_ONLY='true'
```

模型或依赖不可用时，`semantic_event_features.status` 会明确记录 `unavailable`；未启用时记录 `disabled`。系统不会静默下载，也不会回退到固定商品模板。人工评测可通过 `/semantic-review` 或 `POST /api/events/{event_id}/semantic-label` 保存；`GET /api/semantic/evaluation?k=10` 返回 embedding 与趋势规则基线的 Precision@K、类目准确率、弃权率、无消费意义标签占比、重复候选精度和模型版本对比。

当前工作机已经在 `.venv` 安装 ML extra，并将固定 revision `614241f622f53c4eeff9890bdc4f31cfecc418b3` 缓存到被 Git 忽略的 `data/models`。7 条覆盖性人工样本上，embedding Precision@5 为 0.20，与趋势规则基线 0.20 持平；原 0.84 去重阈值的 11 对候选均为误报，因此默认候选阈值收紧为 0.90。样本太小且效果未超过基线，`ENABLE_EMBEDDINGS` 继续默认为 `false`，不得据此自动创建线索或合并事件。

## 评分

趋势分：跨源覆盖 30%、榜单排名 25%、上升速度 20%、持续性 15%、新鲜度 10%。首次采集没有历史基线时，上升速度取中性值 50。

假设分：痛点 25%、购买意图 20%、人群清晰度 15%、时机 15%、可行性 15%、差异化 10%。它只表示新闻到产品假设的合理度，不冒充市场数据。

市场分：搜索需求 20%、购买意图 15%、竞争机会 15%、单位经济性 20%、差异化 10%、执行可行性 10%、时机持续性 5%、证据完整度 5%。缺失维度按零贡献计算，不重新分配权重。

已验证推荐分：仅当市场验证状态为 `completed` 且市场分存在时计算，公式为趋势分 25% + 市场分 75%，再扣除产品风险。缺少或只有部分市场证据时保持为空，绝不使用假设分代理；阻断风险不得成为推荐。证据置信度继续独立显示。

## 重要边界

- 热点关注度不等于购买需求，所有建议都需要人工验证。
- Google Trends、Reddit、Product Hunt 等只提供趋势或讨论信号，不等于 Amazon 销量；当前不会把搜索热度伪装成销售数据。
- Amazon SP-API 适合在已有关键词、ASIN 或 SKU 后验证目录、价格和报价，不是公共爆品发现源；Demo 当前只生成验证计划，尚未要求卖家授权。
- 当前没有可靠、无需登录或商业授权的小红书公开源。
- 热榜页面经常有 302/403 或反爬；系统保留失败状态，不绕过验证码、登录或付费墙。
- 默认仅允许本机执行采集、审核和推送；配置 `ADMIN_TOKEN` 后可用于可信内网。公网部署仍需要正式身份认证与访问限速。
- 数据库默认在 `data/trends.db`，原始响应可能受版权和站点条款约束，不应作为公开内容镜像。
