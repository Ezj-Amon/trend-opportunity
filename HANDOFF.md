# 新会话交接：全球趋势驱动新品机会系统

更新时间：2026-07-17  
工作目录：`D:\code\xpzs`  
当前分支：`main`  
当前 HEAD：`c5197d6 Implement comprehensive application updates`  
重要：本文描述的大量变更尚未提交，工作区是脏的，不要重置或覆盖。

## 0. 本次续做结果（2026-07-17，后续状态以本节为准）

原“当前卡在哪里”和“下一步计划”中的工程项已经继续完成：

- 新增语义重复候选与人工反馈快照，候选不自动合并；新增 `/semantic-review`、重复候选 API、真实样本标签和模型版本对比指标。
- 新增人工 OpportunitySignal 创建入口，必须引用本事件证据，仍需审核为 `follow_up` 才能进入商品构思。
- 新增独立 `product_hypotheses`、反馈历史、`ProductHypothesisGenerator` Protocol、人工工作台和实体商品/证据/风险门。
- 旧 LLM 分析提示已收缩为只允许输出 OpportunitySignal；直接输出旧 `OpportunityDraft` 会被拒绝。
- 新增独立 `market_evidence`、`MarketplaceDataProvider`、Seller Central CSV Provider 和 `validated_recommendations`。
- 最终推荐只在市场证据 `completed`、单位经济分和证据分至少为 3、风险为 low/medium 时产生，并保存完整对象链快照。
- 已用真实库建立 7 条小型人工评测基线，覆盖正向实体机会、无实体商品、消费关联弱、短时、高风险、软件/服务和证据不足；固定 revision 的特征状态为 `ready`，但运行开关仍保持 `ENABLE_EMBEDDINGS=false`。
- 机器资源已核对：16 GB RAM、i7-14700HX、RTX 4070 Laptop。已在 `.venv` 安装 ML extra，并把固定 revision `614241f622f53c4eeff9890bdc4f31cfecc418b3` 缓存到 Git 忽略的 `data/models`（约 470 MB）。
- 真实模型：384 维；首次联网准备约 170 秒；纯本地冷启动约 48 秒；工作集约 791 MB、私有提交约 2.64 GB。7 条样本首条含加载约 40 秒，后续每条约 0.1–0.7 秒。
- 真实小样本指标：Embedding Precision@5 0.20，趋势规则基线 0.20，正向样本类目 Top-1 正确；未超过基线。0.84 去重阈值产生 11 对误报，均已人工反馈为 `not_duplicate`，默认阈值收紧为 0.90。
- 新增链路回归测试后，核心测试 33 项、含 live 来源的全量测试 35 项均通过；`.venv` 的 Windows `tzdata` 依赖已加入项目声明。所有变更仍未提交。

模型准备和小样本评测已完成，但效果没有超过趋势规则基线。因此 `ENABLE_EMBEDDINGS=false` 仍是正确线上默认值；需要继续积累数百条高质量标签和真实重复/非重复对后再重新评测，不得上线自动排序、自动创建线索或自动合并。

## 1. 我们在做什么

这个项目不是“把热点关键词转换成商品名”的生成器。目标是：

> 持续收集全球新闻、搜索和社区趋势信号，尽早识别可能产生新实体消费品需求的变化，再对具体商品假设使用 Amazon 等平台证据进行验证。

第一阶段只聚焦适合电商销售的实体消费品，不包含软件、订阅、咨询、课程、资料包、危险品、医疗功效商品和未经授权的事件周边。

已经确定的产品对象链路：

```text
TrendEvent（趋势事件，事实层）
  -> OpportunitySignal（新品机会线索，判断层）
  -> ProductHypothesis（具体商品假设，构思层）
  -> MarketEvidence（平台市场证据，验证层）
  -> ValidatedRecommendation（已验证选品推荐，决策层）
```

越靠后证据门槛越高。缺少某层证据时允许为空、停止或弃权，不允许使用规则分、模型置信度、主题词或假设分补成“看似完整”的推荐。

完整产品边界以 `docs/product-boundary-and-architecture.md` 为准。`README.md` 已同步当前方向。

## 2. 已经完成了什么

### 2.1 原来已有且继续保留的能力

- 国内外真实趋势源采集、原始响应审计、来源健康状态。
- 热榜条目去重、事件聚类、趋势分和来源证据抓取。
- 采集进度透明度：阶段、百分比、数据源完成/失败数、记录数、事件数、已分析数、耗时和 ETA。
- Seller Central Product Opportunity Explorer / Brand Analytics 原始 CSV 解析。
- 飞书推送、幂等投递、人工审核和 7/30 天结果记录。
- 风险门、安全事件弃权和市场分维度解析。

### 2.2 Phase 0：止血，已完成

核心目标是阻止旧架构继续向用户输出伪推荐。

- `app/analysis.py`
  - `local-rules-v2` 现在统一主动弃权。
  - 本地规则只保留事实层安全检查，不再从 `CATEGORIES` 新闻关键词生成固定商品/服务模板。
  - 允许事件只有事实层、没有机会线索或商品。
- `app/scoring.py`
  - `calculate_final_score` 不再用 `hypothesis_score` 代理市场分。
  - 只有 `validation_status == completed` 且 `market_score` 存在时才计算推荐分。
  - 缺失或部分市场证据时返回 `None`。
- `app/db.py`
  - 新增可空的 `validated_recommendation_score`。
  - 旧 `final_score` 为兼容字段；无验证时写 0，但用户可见和资格判断使用新的可空字段。
- `app/reports.py`、`app/feishu.py`、首页模板
  - 首页和每日飞书 Top 3 已改为事实层“趋势信号摘要”。
  - 不再从旧 `product_opportunities` 生成未验证选品榜。
  - 文案明确趋势热度不是需求、销量或推荐。
- `app/main.py`
  - `PRODUCT_HYPOTHESIS_WORKBENCH_ENABLED = False`，市场验证自动队列暂时返回空。
  - 不再从新闻关键词或规则关键词自动选 Amazon 查询词。
  - 单条商品推送必须同时满足：人工批准、市场验证完成、市场分存在、`validated_recommendation_score` 存在、风险不阻断。
- `app/templates/event.html`
  - 旧商品输出明确标为“商品假设（旧版）/迁移期审计”，不是推荐。

### 2.3 Phase 1：OpportunitySignal 主链路，已完成

#### 数据结构

`app/db.py` 新增：

- `opportunity_signals`
  - `event_id`、`analysis_id`
  - `change_type`
  - `consumer_relevance_score`
  - `product_opportunity_score`
  - `target_users_json`
  - `new_scenarios_json`
  - `unmet_needs_json`
  - `related_product_categories_json`
  - `durability`
  - `lead_time_fit`
  - `evidence_ids_json`
  - `confidence`
  - `missing_evidence_json`
  - `review_status`
  - `engine`、`model`、`version`
  - 创建和更新时间
- `opportunity_signal_feedback`
  - 保存反馈类型、备注和完整 `snapshot_json`。

`app/analysis.py` 新增 `OpportunitySignalDraft` Pydantic Schema。`AnalysisOutput` 支持：

- `signals=[]`
- `opportunities=[]`

两者都可为空。模型引用的 signal/opportunity `evidence_ids` 都必须存在于本次证据集合。

#### 管道和版本

- `app/pipeline.py` 可以持久化 0 到 3 条 OpportunitySignal。
- 新分析会把同事件旧线索标为 `superseded`，但不会删除反馈历史。
- 分析版本变化时，旧线索也会失效，避免旧模型输出继续进入当前页面。
- 每条线索保存引擎、模型和完整分析版本。

#### 页面和 API

- `/`：全球趋势；事件行显示 OpportunitySignal 的线索排序分，不再显示旧商品机会分。
- `/signals`：新品机会线索页面。
- `/feedback`：待反馈队列和最近反馈历史。
- `/events/{id}`：事件详情增加 OpportunitySignal 判断层，旧商品假设放在后面。
- `GET /api/opportunity-signals`
- `POST /api/opportunity-signals/{signal_id}/feedback`

反馈类型固定为：

- `follow_up`：值得跟进
- `no_physical_product`：没有实体商品机会
- `weak_consumer_relevance`：消费关联弱
- `too_short_term`：过于短期
- `wrong_category`：类目错误
- `insufficient_evidence`：证据不足

每次反馈快照包含当时的完整 signal、TrendEvent 趋势特征、引用证据、引擎、模型、版本和人工备注，可直接作为后续训练/评测数据。

### 2.4 Phase 2 第一段：可选语义基线，已完成

#### 可选依赖和配置

- `pyproject.toml` 新增独立 `ml` extra：`sentence-transformers>=3,<6`。
- 默认安装不引入 PyTorch 或 sentence-transformers。
- 默认模型：`intfloat/multilingual-e5-small`。
- `app/config.py` 和 `.env.example` 新增：
  - `ENABLE_EMBEDDINGS=false`
  - `EMBEDDING_MODEL_ID`
  - `EMBEDDING_MODEL_REVISION`
  - `EMBEDDING_CACHE_DIR`
  - `EMBEDDING_LOCAL_FILES_ONLY=true`
  - `SEMANTIC_FEATURE_VERSION`
- 默认测试和默认运行不会下载模型。

#### 语义实现

新增 `app/semantic.py`：

- `TextEmbedder` Protocol。
- 懒加载 `SentenceTransformerEmbedder`。
- 模型只在真正调用 `encode` 时导入 sentence-transformers/torch。
- 本地缓存缺失或依赖缺失时抛出 `EmbeddingUnavailable`。
- multilingual-e5 输入使用 `query:` / `passage:` 前缀。
- `semantic_input` 使用事件标题和短证据摘要。
- 保存 SHA-256 输入哈希。
- 3 条正向新品机会原型。
- 3 条负向原型：娱乐/短时噪声，悲剧/高风险，软件/服务/资料产品。
- 6 个实体商品类目原型：家居收纳、出行户外、厨房餐饮、宠物用品、个护整理、汽车配件。
- 输出正向相似度、负向相似度、差值和类目 Top 3。
- 相似度只叫“相似度”，绝不能叫市场概率或成功率。

#### 语义数据和显式降级

`app/db.py` 新增：

- `semantic_event_features`
  - event、model id、model revision、input hash、feature version
  - `status`: `ready` / `disabled` / `unavailable` / `failed`
  - embedding JSON、类目候选、正负相似度、错误信息
- `semantic_evaluation_labels`
  - 人工正负标签、期望类目、备注

`app/pipeline.py::_persist_semantic_features`：

- 每个被分析事件都会写一条明确语义状态。
- 未启用写 `disabled`。
- 模型/依赖不可用写 `unavailable`。
- 未知异常写 `failed`。
- 成功写 `ready` 和向量/原型特征。
- 如果之前是 disabled/unavailable，后来模型可用，会重试而不是被旧缓存永久挡住。
- 任何失败都不会回退到固定商品模板。

事件详情页会展示语义状态、模型 revision、正负原型相似度和类目候选，并明确“相似度不是市场概率”。

#### 离线评测

- `POST /api/events/{event_id}/semantic-label`
  - 支持 `positive`、`no_physical_product`、`weak_consumer_relevance`、`too_short_term`、`insufficient_evidence`。
- `GET /api/semantic/evaluation?k=10`
  - Opportunity Precision@K
  - 类目准确率
  - 弃权率
  - 无消费意义标签占比
- `app/semantic.py` 还有纯函数 `opportunity_precision_at_k` 和 `duplicate_rate`。
- 测试使用纯假 Embedder，不需要真实模型或网络。

## 3. 当前卡在哪里

没有外部依赖型阻塞，但以下工作尚未完成：

1. **真实模型尚未安装或缓存。** 当前默认 `ENABLE_EMBEDDINGS=false`，数据库会记录 `disabled`。不要把页面为空或 disabled 误判成代码失败。
2. **当前没有可靠的自动 OpportunitySignal 生产器。** `local-rules-v2` 按设计弃权；因此没有模型/人工输入时 `/signals` 为空是正确结果。
3. **语义向量尚未接入事件聚类决策。** 当前已用于事件语义特征、正负机会原型和类目检索，但还没有形成“重复候选 -> 人工确认 -> 调整聚类阈值”的闭环。
4. **离线评测集几乎为空。** 表和 API 已有，需要人工标注真实事件，才能得出有意义的 Precision@K、类目准确率和弃权率。
5. **旧 LLM 路径仍直接输出 `OpportunityDraft`。** 它是迁移期兼容代码，还没有重构成 Phase 3 的 `ProductHypothesisGenerator`，不能把它当成最终架构。
6. **旧 `product_opportunities` 仍混合 ProductHypothesis 与历史字段。** Phase 0 已阻止其冒充推荐，但稳定的 `product_hypotheses` / `market_evidence` / `validated_recommendations` 独立结构尚未建立。
7. **市场验证工作台处于后置状态。** `PRODUCT_HYPOTHESIS_WORKBENCH_ENABLED=False`；Seller Central 解析能力仍在，但自动队列为空。
8. **没有自动语义去重候选表、模型版本对比页或评测仪表盘。** 当前只有数据库、事件详情和 JSON API。

## 4. 验证状态

最后验证时间：2026-07-17。

```text
python -m compileall -q app
  -> passed

python -m pytest -q tests/test_core.py
  -> 31 passed（Phase 2 评测 API 加入前一次为 31；之后全量运行中所有核心测试均通过）

python -m pytest -q
  -> 32 passed, 1 failed
  -> 唯一失败是 NewsNow 百度实时接口上游 HTTP 500

python -m pytest -q tests/test_live_sources.py::test_real_newsnow_sources_return_current_items
  -> 1 passed（同一实时源立即复测恢复）

git diff --check
  -> passed，仅有 Windows LF/CRLF 提示
```

说明：全量测试包含真实外部源，NewsNow/Google Trends 的临时 500、超时或限流不等于代码回归。先单独复测失败的 live case，再判断。

`.venv\Scripts\python.exe -m pytest` 之前会报没有 pytest，因为虚拟环境未安装 dev extra。可使用系统 `python`，或明确安装 `.[dev]`。

## 5. 下一步计划

### 第一优先：完成 Phase 2 的语义去重与评测闭环

1. 不要立即联网下载模型。先确定机器资源、缓存位置和固定 revision。
2. 如用户明确允许，再执行：

   ```powershell
   pip install -e ".[ml]"
   # 显式准备模型缓存后再设置：
   $env:ENABLE_EMBEDDINGS='true'
   $env:EMBEDDING_LOCAL_FILES_ONLY='true'
   ```

3. 先在小样本上运行真实 multilingual-e5-small，检查加载时间、内存、向量维度和缓存命中。
4. 新增“语义重复候选”结构，不要直接自动合并：
   - event A/B
   - cosine similarity
   - model/revision/feature version
   - lexical similarity
   - market/language
   - review status 和人工合并反馈
5. 用语义候选辅助聚类评审，效果稳定后才考虑自动合并阈值。
6. 从 `data/trends.db` 抽取一批真实事件建立人工评测集，至少覆盖：
   - 值得跟进的实体新品机会
   - 无实体商品机会
   - 消费关联弱
   - 短时热点
   - 高风险/悲剧
   - 软件/服务类
7. 对比规则基线与 embedding 基线的 Precision@K、重复率、无消费意义事件占比、类目准确率和弃权率。
8. 在效果没有超过基线前，不要让 embedding 自动创建 OpportunitySignal。

### 第二优先：让 OpportunitySignal 可稳定产生

建议先采用保守流程：

1. embedding 只产生候选特征和原型匹配。
2. 人工从候选事件创建/补全 OpportunitySignal，或使用可审计的弱分类器。
3. 不要用 embedding 自动编造目标用户、场景和未满足需求。
4. 积累数百条高质量反馈后，在冻结向量和趋势特征上训练 Logistic Regression / LightGBM。
5. 只有离线评测优于基线后才替换线上排序。

### Phase 3：ProductHypothesis

1. 新增稳定、独立的 `product_hypotheses` 数据模型，必须引用 `opportunity_signal_id`。
2. 先支持人工从线索创建具体实体商品假设。
3. 定义 `ProductHypothesisGenerator` Protocol。
4. 把现有 AsyncOpenAI 路径收缩成一个 Provider，不再让它直接主导全管道。
5. 输出必须通过实体商品约束、风险校验、Schema 和证据引用校验。
6. 只有具体、审核后的 ProductHypothesis 才能产生 Amazon 查询词。

### Phase 4：MarketEvidence 与最终推荐

1. 把现有 Seller Central CSV 封装成 `MarketplaceDataProvider`。
2. 独立存储 MarketEvidence，不再塞入旧机会表。
3. 完成平台证据、风险和经济性门槛后才创建 ValidatedRecommendation。
4. 每条最终推荐必须能回溯：TrendEvent -> OpportunitySignal -> ProductHypothesis -> MarketEvidence。

## 6. 绝对不要再踩的坑

1. **不要把新闻关键词、人物名、技术名当 Amazon 查询词。**
2. **不要恢复或扩充 `CATEGORIES` 固定商品模板。** 当前常量仍有历史残留，但 `local-rules-v2` 已不使用它生成输出；后续最好逐步删除或只保留纯类目弱标签。
3. **不要用 hypothesis_score、规则分、embedding 相似度或模型置信度冒充市场需求。**
4. **不要把 OpportunitySignal 或 ProductHypothesis 称为推荐。**
5. **不要强行填满 Top 3。** 零机会是合法结果。
6. **不要让生成模型替代采集、事实、状态机、审计、评分和市场证据。**
7. **不要让 Agent/Skill 成为唯一事实来源或绕过状态门槛。**
8. **不要在默认测试或服务启动时在线下载 embedding 模型。**
9. **不要静默降级到旧商品模板。** 模型不可用必须保留 explicit unavailable/failed 状态。
10. **不要把 embedding 相似度显示成概率。** 当前 UI 文案已明确是相似度。
11. **不要一开始训练或微调大型深度模型。** 先评测通用 embedding，再训练浅层模型。
12. **不要自动合并高相似事件而不保留证据和人工复核。** 语义相似不等于同一事件。
13. **不要删除或覆盖反馈快照。** 它们是后续训练和版本评估的核心数据。
14. **不要执行破坏性数据库重建而不备份。** `python -m app.cli rebuild` 会清空事件、证据、分析、线索、机会和反馈等派生数据。
15. **不要覆盖当前工作区。** 所有 Phase 0/1/2 变更都尚未提交。
16. **不要擅自提交、重置或清理未跟踪文件。** 用户尚未授权 commit。
17. **Windows 环境不要使用 `rg`。** 仓库指令要求使用 PowerShell 原生命令，如 `Get-ChildItem`、`Select-String`、`Get-Content`。
18. **不要把 live 测试的单次上游 500 直接判为代码失败。** 单独复测并记录来源状态。
19. **不要让禁用状态永久阻塞后续模型启用。** `_persist_semantic_features` 已处理 disabled/unavailable -> 重试；修改缓存逻辑时要保留这个性质。
20. **不要在没有评测标签时宣称模型有效。** 目前 API 可运行不代表指标有统计意义。

## 7. 工作区状态

写完本交接时，所有以下变更均未提交：

```text
M  .env.example
M  README.md
M  app/analysis.py
M  app/config.py
M  app/db.py
M  app/feishu.py
M  app/main.py
M  app/pipeline.py
M  app/reports.py
M  app/scoring.py
M  app/static/app.css
M  app/templates/base.html
M  app/templates/dashboard.html
M  app/templates/event.html
M  app/templates/validation.html
M  pyproject.toml
M  tests/test_core.py
?? HANDOFF.md
?? app/opportunity_signals.py
?? app/semantic.py
?? app/templates/feedback.html
?? app/templates/signals.html
?? docs/product-boundary-and-architecture.md
```

Phase 0/1/2 累计 diff 约为 900+ 行新增、260+ 行删除；不要根据文件数量误以为只有文档变更。

## 8. 新会话建议先执行

```powershell
Set-Location -LiteralPath 'D:\code\xpzs'
git status --short
git diff --check
Get-Content -LiteralPath 'HANDOFF.md' -Raw
Get-Content -LiteralPath 'docs\product-boundary-and-architecture.md' -Raw
python -m compileall -q app
python -m pytest -q tests\test_core.py
```

然后重点阅读：

- 产品边界：`docs/product-boundary-and-architecture.md`
- OpportunitySignal Schema：`app/analysis.py`
- 数据库：`app/db.py`
- 线索解码与反馈枚举：`app/opportunity_signals.py`
- embedding/原型/评测：`app/semantic.py`
- 管道：`app/pipeline.py`
- 页面/API：`app/main.py`
- 线索页：`app/templates/signals.html`
- 反馈页：`app/templates/feedback.html`
- 回归测试：`tests/test_core.py`

最合理的续做入口是：**先建立语义重复候选与人工聚类反馈，不要直接安装大模型、恢复商品模板或开始优化 Amazon 查询词。**
