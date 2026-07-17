# 趋势新闻自动选品助手 Demo

一个使用真实热点数据的个人验证项目：采集国内外热榜、搜索和社区信号，聚类成事件，计算趋势分，抓取来源证据，生成绑定目标 Amazon 站点的产品机会并评分，经过人工审核后可推送到飞书。

## 当前能力

- 国内数据：默认通过 NewsNow 公共 API 采集微博、知乎、百度、抖音、头条、B 站、酷安和贴吧。
- 海外数据：默认采集 US/GB/DE/JP Google Trends RSS，以及 Hacker News、Product Hunt、GitHub Trending；后台可按市场筛选。
- 海外社媒：提供 Reddit OAuth 可选适配器，使用 Async PRAW 管理鉴权、分页和限流，不依赖不稳定的匿名抓取。
- Amazon 语义：海外机会显式标注 Amazon.com、Amazon.co.uk、Amazon.de 或 Amazon.co.jp，并保留关键词、竞品、价格带和评论痛点的后续验证项。
- 可追溯：保存每次请求的状态、延迟、原始响应哈希、热榜排名和事件聚类关系。
- 可解释：趋势分和机会总分由代码计算，并显示所有分项。
- 证据优先：尝试读取热榜链接正文；失败时保留真实热榜标题、URL 和 HTTP 状态，不伪造正文。
- 双分析引擎：配置 OpenAI-compatible API 时使用真实模型；未配置时使用 `local-rules-v1`，界面明确标记为待验证推断。
- 安全门：死亡、犯罪和受害者相关敏感事件不生成商业化建议。
- 主动弃权：本地规则无法识别稳定消费类别时返回零机会，不用通用模板凑结果。
- 人工审核：机会通过后才能推送飞书，重复推送具有幂等保护。
- 双榜摘要：控制台分别展示中国信号 Top 3 和海外信号 Top 3；按事件与产品方向去重，允许当天无合格候选。
- 市场验证分层：产品假设分、Amazon 一方市场分和最终排序分分开保存；未录入 Product Opportunity Explorer / Brand Analytics 时明确显示 `unavailable`，不补写搜索量、竞争或利润数据。
- 产品风险门：合规、IP、物流、季节性、供应链和单位经济性风险结构化记录，阻断级机会不能进入榜单、通过审核或推送。
- 反馈闭环：支持保存审核原因、7 天结果和 30 天结果，为后续复盘提供标签。

## 快速启动

当前环境已有依赖时：

```powershell
python -m app.cli run
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

然后访问 <http://127.0.0.1:8000>。

聚类规则升级或本地派生数据需要重建时，可执行 `python -m app.cli rebuild`。该命令保留原始来源快照与条目，清空事件、证据、分析、机会和推送记录，再进行一次新的真实采集；运行前应先备份需要保留的审核结果。

从全新 Python 3.12 环境安装：

```powershell
python -m pip install -e .
```

所有配置均为环境变量，示例见 `.env.example`。应用不会自动读取 `.env`，避免无意加载密钥；PowerShell 中可用 `$env:变量名='值'` 设置。

## 海外源与 Reddit

默认海外发现层无需账户：

```powershell
$env:GOOGLE_TRENDS_GEOS='US,GB,DE,JP'
$env:OVERSEAS_ANALYSIS_TOP_N='5'
python -m app.cli run
```

需要加入 Reddit 消费讨论时，请创建 Reddit API 应用并配置 OAuth 凭证：

```powershell
$env:REDDIT_CLIENT_ID='...'
$env:REDDIT_CLIENT_SECRET='...'
$env:REDDIT_USER_AGENT='TrendOpportunityLab/0.2 by your-reddit-name'
$env:REDDIT_SUBREDDITS='BuyItForLife,gadgets,HomeImprovement,shutupandtakemymoney'
python -m app.cli run
```

未配置凭证时不会请求 Reddit，也不会把 Reddit 标成失败源。完整的成熟项目、核心代码、许可证、Issue 和实测比较见 [海外数据源调研](docs/overseas-source-research.md)。

默认情况下，管理写接口只接受本机请求，并校验浏览器 `Origin`。如果需要从可信内网访问，必须设置 `ADMIN_TOKEN`，并在浏览器控制台执行 `localStorage.setItem('trendAdminToken', '同一个令牌')`。这只是 Demo 级保护；公网部署仍应接入正式身份认证和限流。

## 使用真实模型

```powershell
$env:OPENAI_API_KEY='你的密钥'
$env:OPENAI_MODEL='gpt-4.1-mini'
# 使用兼容服务时再设置：
$env:OPENAI_BASE_URL='https://example.com/v1'
python -m app.cli run
```

模型输出必须通过 Pydantic Schema，并且只能引用数据库中存在的证据 ID。如果模型失败，运行会明确记录 `local-rules` 引擎，不会冒充模型结果。

## 飞书推送

创建飞书群自定义机器人后设置：

```powershell
$env:FEISHU_WEBHOOK_URL='https://open.feishu.cn/open-apis/bot/v2/hook/...'
$env:FEISHU_SECRET='机器人签名密钥（未开启签名可不填）'
$env:PUBLIC_BASE_URL='http://你的可访问地址:8000'
```

在事件详情页先点击“通过”，再点击“推送飞书”。Webhook 和 Secret 只从环境变量读取，不写入 SQLite。

首页还提供“推送双榜到飞书”：一次发送中国信号 Top 3 和海外信号 Top 3。摘要不会强制凑满三个候选，并会标出市场验证缺口。
升级前生成的 `opportunity-v1` 机会不会混入新榜单；升级后请运行一次采集，生成带风险和市场验证状态的 `opportunity-v2` 候选。

## 市场验证

当前优先使用专业卖家账户中的 Amazon 一方数据，不依赖卖家精灵或 SP-API。打开 `/validation` 工作台，可以按目标站点查看待验证队列，并直接上传“商机探测器”和“品牌分析 → 热门搜索词”的中文 Seller Central 原始 CSV；程序会跳过文件开头的筛选条件，精确匹配查询词并生成保守的部分市场评分。也保留中文标准化 CSV 作为人工补分入口。完整操作见 [Amazon 一方数据验证流程](docs/amazon-first-party-validation.md)。

美国站待验证机会必须先具备具体英文商品查询词。过短、纯中文或类似 `AI` 的泛化词会标为 `needs_keyword`，不会直接交给人工查询；查询词可在工作台修正，修正后旧市场验证自动失效。

程序明确区分事件的信号来源市场和产品的目标 Amazon 站点。默认目标站点由 `AMAZON_DEFAULT_MARKETPLACE` 控制；更换目标站点会让旧站点验证失效并把审核状态重置为待审核。

程序同时保留可插拔 `MarketValidator` 契约和单条工作流回写 API：

```text
POST /api/opportunities/{opportunity_id}/validation
```

也提供 `GET /api/opportunities/pending-validation` 给后续 Skill/Agent 读取待验证队列。页面上的“录入市场验证”可用于单条手动验证。八个维度依次为搜索需求、购买意图、竞争机会、单位经济性、差异化、执行可行性、时机持续性和证据完整度；未知项必须留空。

审核时可填写原因，机会详情页还可记录 7 天和 30 天结果。写接口仍遵守本机或 `ADMIN_TOKEN` 限制。

## 测试

```powershell
python -m pytest -q
python -m pytest -q -m live
```

`live` 测试会真实访问 NewsNow 和 Google Trends RSS，不使用 Mock。由于它依赖外部服务，网络或上游故障会真实导致测试失败。Reddit 需要个人 OAuth 凭证，因此不放入无凭证 CI 测试。

## 评分

趋势分：跨源覆盖 30%、榜单排名 25%、上升速度 20%、持续性 15%、新鲜度 10%。首次采集没有历史基线时，上升速度取中性值 50。

假设分：痛点 25%、购买意图 20%、人群清晰度 15%、时机 15%、可行性 15%、差异化 10%。它只表示新闻到产品假设的合理度，不冒充市场数据。

市场分：搜索需求 20%、购买意图 15%、竞争机会 15%、单位经济性 20%、差异化 10%、执行可行性 10%、时机持续性 5%、证据完整度 5%。缺失维度按零贡献计算，不重新分配权重。

最终排序分：趋势分 25% + 市场分 75%，再扣除验证缺失和产品风险惩罚。市场数据完全缺失时，暂用假设分作为带有 30 分不确定性惩罚的代理值；界面始终标为 `unavailable`。阻断风险直接得到零分。证据置信度继续独立显示。

## 重要边界

- 热点关注度不等于购买需求，所有建议都需要人工验证。
- Google Trends、Reddit、Product Hunt 等只提供趋势或讨论信号，不等于 Amazon 销量；当前不会把搜索热度伪装成销售数据。
- Amazon SP-API 适合在已有关键词、ASIN 或 SKU 后验证目录、价格和报价，不是公共爆品发现源；Demo 当前只生成验证计划，尚未要求卖家授权。
- 当前没有可靠、无需登录或商业授权的小红书公开源。
- 热榜页面经常有 302/403 或反爬；系统保留失败状态，不绕过验证码、登录或付费墙。
- 默认仅允许本机执行采集、审核和推送；配置 `ADMIN_TOKEN` 后可用于可信内网。公网部署仍需要正式身份认证与访问限速。
- 数据库默认在 `data/trends.db`，原始响应可能受版权和站点条款约束，不应作为公开内容镜像。
