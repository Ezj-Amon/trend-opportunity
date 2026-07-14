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

## 测试

```powershell
python -m pytest -q
python -m pytest -q -m live
```

`live` 测试会真实访问 NewsNow 和 Google Trends RSS，不使用 Mock。由于它依赖外部服务，网络或上游故障会真实导致测试失败。Reddit 需要个人 OAuth 凭证，因此不放入无凭证 CI 测试。

## 评分

趋势分：跨源覆盖 30%、榜单排名 25%、上升速度 20%、持续性 15%、新鲜度 10%。首次采集没有历史基线时，上升速度取中性值 50。

机会分：痛点 25%、购买意图 20%、人群清晰度 15%、时机 15%、可行性 15%、差异化 10%。每项 1–5 分先归一化，再由代码计算 0–100 总分。证据置信度独立显示，不混入机会分。

## 重要边界

- 热点关注度不等于购买需求，所有建议都需要人工验证。
- Google Trends、Reddit、Product Hunt 等只提供趋势或讨论信号，不等于 Amazon 销量；当前不会把搜索热度伪装成销售数据。
- Amazon SP-API 适合在已有关键词、ASIN 或 SKU 后验证目录、价格和报价，不是公共爆品发现源；Demo 当前只生成验证计划，尚未要求卖家授权。
- 当前没有可靠、无需登录或商业授权的小红书公开源。
- 热榜页面经常有 302/403 或反爬；系统保留失败状态，不绕过验证码、登录或付费墙。
- 默认仅允许本机执行采集、审核和推送；配置 `ADMIN_TOKEN` 后可用于可信内网。公网部署仍需要正式身份认证与访问限速。
- 数据库默认在 `data/trends.db`，原始响应可能受版权和站点条款约束，不应作为公开内容镜像。
