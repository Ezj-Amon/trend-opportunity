# 海外趋势与 Amazon 选品数据源调研（2026-07-14）

> **Historical / Archived**：本文是历史调研记录，不再是当前实现依据。当前业务流程以 [业务流程合同](../workflow-contract.md) 为准，当前技术结构以 [技术架构](../architecture.md) 为准。

本调研不是只读 README。检查范围包括仓库目录、核心采集代码、依赖、最新提交、Issue、许可证，并对候选公开端点进行了真实请求。

## 结论先行

海外“趋势发现”与 Amazon“商品验证”必须分层：

1. 趋势发现：Google Trends、Reddit 消费讨论、Product Hunt、Hacker News、GitHub Trending。
2. 候选验证：获得候选关键词或 ASIN 后，再调用 Amazon SP-API 的 Catalog/Pricing/Data Kiosk。
3. 不能用 SP-API 替代趋势发现；它主要回答已知 SKU/ASIN 的价格、报价、目录和卖家经营问题。

Demo 默认使用无需账户且已真实验证的 Google Trends RSS，以及 NewsNow 已维护的 Hacker News、Product Hunt、GitHub Trending。Reddit 仅在提供 OAuth 凭证后通过 Async PRAW 启用。Amazon SP-API 留作凭证型验证层，不抓取 Amazon Best Sellers 页面。

## 项目比较

### NewsNow

- 仓库：[ourongxing/newsnow](https://github.com/ourongxing/newsnow)
- 状态：约 2.1 万 Star，2026 年仍持续提交，MIT。
- 实际代码：`server/sources/hackernews.ts` 使用 Cheerio 解析 Hacker News；`server/sources/github.ts` 解析 GitHub Trending；`server/sources/producthunt.ts` 优先使用 Product Hunt GraphQL API，失败时回退官方 Atom Feed；`server/utils/source.ts` 提供统一 RSS/RSSHub 适配器。
- 值得借鉴：统一输出、单源失败隔离、Product Hunt API 到 Feed 的降级策略、缓存友好的稳定 ID。
- 不直接复制：它把所有条目视为同一种热榜，没有市场、语言、信号类型和 Amazon marketplace 语义。
- 已知问题：公开 Issue 中长期存在 Hacker News/Product Hunt 可用性、页面结构变化和反爬问题，因此必须保存每次来源健康，不能假设聚合端永远可用。[NewsNow Issues](https://github.com/ourongxing/newsnow/issues)

### RSSHub

- 仓库：[DIYgod/RSSHub](https://github.com/DIYgod/RSSHub)
- 状态：约 4.5 万 Star、1 万 Fork，2026-07-14 仍有提交，AGPL-3.0。
- 实际代码：`lib/routes/producthunt/today.tsx` 从页面中的 Apollo 数据提取榜单，并对单品 GraphQL 数据做缓存；`lib/routes/google/news.tsx` 解析地区化 Google News；核心依赖包含 Cheerio、缓存、限流、代理和浏览器运行时。
- 值得借鉴：路由元数据、是否需配置/浏览器/反爬的能力声明、缓存和失败可观测性。
- 不直接复制：完整 RSSHub 依赖和部署面远超个人 Demo；其 AGPL 网络服务条款也不适合直接拷贝代码进入当前 MIT 风格的小应用。当前仓库已无 Reddit 路由，说明匿名社媒抓取并非稳定能力。
- 已知问题：数百个开放 Issue，大量问题来自站点 DOM 或反爬变化；适合自托管为独立基础设施，不适合嵌入当前进程。[RSSHub](https://github.com/DIYgod/RSSHub)

### Async PRAW / PRAW

- 仓库：[praw-dev/asyncpraw](https://github.com/praw-dev/asyncpraw)、[praw-dev/praw](https://github.com/praw-dev/praw)
- 状态：2026 年仍活跃；Async PRAW 已发布 8.x；BSD-2-Clause。
- 实际代码：ListingGenerator 负责 Reddit listing 分页并遵循 API 返回的 `after`；Subreddit listing 封装 `hot/top/new`；依赖层由 `asyncprawcore/prawcore` 处理 OAuth、速率限制和错误。
- 值得借鉴/采用：直接作为 Reddit OAuth 客户端，不自行实现 token 刷新、分页和限流。
- 不适合作为默认无配置源：Reddit 要求 OAuth 和合规的 User-Agent；匿名 `.rss/.json` 请求可出现 429，不能承诺稳定采集。[Async PRAW OAuth 文档](https://asyncpraw.readthedocs.io/en/latest/getting_started/authentication.html)、[Reddit Developer Guidelines](https://developers.reddit.com/docs/guidelines)

### pytrends

- 仓库：[GeneralMills/pytrends](https://github.com/GeneralMills/pytrends)
- 状态：约 3.7 千 Star，但仓库已归档；最后有效维护停在 2024 年。
- 实际代码：`pytrends/request.py` 调用 Google 未公开的 `/api/explore`、`/api/dailytrends`、`/api/realtimetrends` 等内部端点，并自行处理 cookie、代理、重试和 429。
- 不采用：Issue #631 报告持续 429，#638 报告 Google 端点变化，#636 是维护者退出。把这些内部端点复制进项目只会重复维护逆向接口。
- 替代：默认使用 Google Trends 官方公开 RSS；如果获得 Google Trends API Alpha 权限，再增加正式 API 适配器。[Google Trends API Alpha](https://developers.google.com/search/apis/trends)

### Amazon Selling Partner API Samples

- 仓库：[amzn/selling-partner-api-samples](https://github.com/amzn/selling-partner-api-samples)
- 状态：Amazon 官方维护，2026 年持续更新，MIT-0。
- 实际代码：`pricing/FetchPriceRecipe.java` 对已知 SKU 调用 Product Pricing API 并提取 landed/listing/shipping price；`RetrieveEligibleOffersRecipe.java` 从 pricing notification 中按 ASIN、Seller、fulfillment type 匹配报价。官方示例也使用 `getCompetitiveSummary` 获取 Featured Offer、最低报价和参考价。
- 值得借鉴：凭证隔离、marketplaceId 显式传递、批量 ASIN、通知驱动更新、限流/失败状态。
- 不用于第一阶段发现：需要专业卖家账户、应用角色和授权，输入通常是已知 SKU/ASIN；它不是公共“爆品榜”。[官方样例](https://github.com/amzn/selling-partner-api-samples)、[竞争价格用例](https://github.com/amzn/selling-partner-api-samples/discussions/100)

### snscrape / Twint

- 结论：不采用。
- 原因：X/Twitter 登录墙和接口变化使公开抓取长期失效；snscrape 维护者明确说明 Twitter 搜索抓取不可用，Twint 的相关故障更早出现。依赖浏览器登录、保存用户 Cookie 或规避限制不适合作为此 Demo 的默认能力。[snscrape #1037](https://github.com/JustAnotherArchivist/snscrape/issues/1037)

## 实测结果

2026-07-14 在当前环境直接请求：

- NewsNow Hacker News：HTTP 200，30 条。
- NewsNow Product Hunt：HTTP 200，20 条。
- NewsNow GitHub Trending：HTTP 200，11 条。
- Google Trends RSS：US/GB/DE/JP 均 HTTP 200，各 10 条。
- Product Hunt Atom Feed：HTTP 200。
- Reddit 单个 subreddit RSS 曾返回 200，但连续/组合请求出现 429，因此不列为可靠默认源。

## 本项目设计取舍

- 借鉴 NewsNow 的统一适配器与 Feed 降级思路，但在数据库中新增 `market`、`language`、`signal_type`。
- 使用 Google Trends RSS 作为搜索需求信号；不嵌入已归档 pytrends。
- 使用 Async PRAW 作为可选 Reddit OAuth 适配器；不编写匿名 Reddit scraper。
- 不复制 RSSHub 路由代码；保留将来连接独立自托管 RSSHub 的可能性。
- Amazon 机会输出显式绑定 marketplace；将来 SP-API 接入发生在“候选验证”阶段，不和趋势采集混在一起。
- 海外事件有独立分析配额，避免被国内跨平台热搜的覆盖分完全挤出 Top-N。
