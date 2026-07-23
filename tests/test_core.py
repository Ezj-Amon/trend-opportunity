from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx
from fastapi.testclient import TestClient

import app.cli as cli_app
import app.main as main_app
import app.pipeline as pipeline_module
from app.amazon import is_search_term_ready, pick_search_term
from app.amazon_validation import (
    COLUMN_LABELS,
    build_raw_amazon_validation,
    parse_validation_csv,
)
from app.clustering import normalize_title, should_merge, title_similarity
from app.config import Settings
from app.db import Database
from app.deduplication import (
    collapse_unworked_duplicate_opportunities,
    deduplicate_events,
    deduplicate_opportunities,
    normalized_identity,
)
from app.evidence import EvidenceResult, fetch_evidence
from app.evidence_bundle import (
    build_evidence_bundle,
    calculate_evidence_quality,
    persist_evidence_bundle,
)
from app.evidence_types import EvidenceType, FetchStatus, normalize_fetch_status
from app.evidence_collectors import (
    PublicNewsSearchCollector,
    RelatedNewsCollector,
    related_news_targets,
)
from app.evidence_quality import canonicalize_public_url, registrable_domain
from app.event_research import build_event_research_view, select_key_evidence
from app.market_validation import MarketScores
from app.opportunity_assessment import (
    CloudOpportunityAssessmentProvider,
    OpportunityAssessmentDraft,
    validate_assessment_evidence,
)
from app.pipeline import Pipeline
from app.reports import build_daily_digest, is_validated_recommendation
from app.research import (
    ResearchBudget,
    ResearchRunCompleteInput,
    ResearchRunInput,
    ResearchToolResultInput,
    complete_research_run,
    record_research_tool_call,
    start_research_run,
)
from app.research_candidates import (
    candidate_from_event,
    is_commercial_research_blocked,
    persist_research_candidate,
)
from app.research_screening import (
    persist_research_screening,
    rescreen_pending_research_candidates,
    screen_research_event,
)
from app.research_tools import ResearchToolExecutor
from app.news_search import GoogleNewsRssSearchProvider, NewsSearchHit
from app.risk import assess_product_risk
from app.scoring import calculate_evidence_confidence, calculate_opportunity_score
from app.scoring import calculate_final_score, calculate_market_score
from app.semantic import (
    CATEGORY_PROTOTYPES,
    EmbeddingUnavailable,
    NEGATIVE_OPPORTUNITY_PROTOTYPES,
    POSITIVE_OPPORTUNITY_PROTOTYPES,
    SemanticFeatureExtractor,
    duplicate_rate,
    opportunity_precision_at_k,
)
from app.semantic_duplicates import create_duplicate_candidates


FULL_ARTICLE_ONE = (
    "Residents described how changing storage constraints disrupted routines across several seasons. "
    "The report compared interviews from multiple neighborhoods and documented repeated purchases, "
    "failed workarounds, and measurable changes in available living space. Researchers also checked "
    "the timeline against public records and explained which observations were independently verified."
)
FULL_ARTICLE_TWO = (
    "A separate newsroom reviewed household diaries, retailer records, and follow-up interviews. "
    "Its investigation found that buyers repeatedly replaced rigid organizers when rooms changed use, "
    "creating avoidable expense and waste. The journalists described the sampling method, publication "
    "dates, contrary examples, and the limits of drawing a broader conclusion from the collected accounts."
)


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "test.db",
        newsnow_base_url="https://newsnow.busiyi.world",
        source_ids=("weibo",),
        research_candidate_top_n=5,
        overseas_research_candidate_top_n=5,
        google_trends_geos=("US",),
        reddit_client_id=None,
        reddit_client_secret=None,
        reddit_user_agent="test-agent",
        reddit_subreddits=("BuyItForLife",),
        schedule_minutes=120,
        enable_scheduler=False,
        openai_api_key=None,
        openai_base_url=None,
        openai_model="unused",
        feishu_webhook_url=None,
        feishu_secret=None,
        public_base_url="http://127.0.0.1:8000",
        admin_token=None,
        enable_public_news_search=False,
    )


def test_cli_run_starts_web_server_without_constructing_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = {}

    class ForbiddenPipeline:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("run command must not construct Pipeline")

    def fake_uvicorn_run(app: str, **kwargs) -> None:
        called.update({"app": app, **kwargs})

    monkeypatch.setattr(cli_app, "Pipeline", ForbiddenPipeline)
    monkeypatch.setattr(cli_app.uvicorn, "run", fake_uvicorn_run)
    cli_app.main(["run"])

    assert called == {"app": "app.main:app", "host": "127.0.0.1", "port": 8000}


def test_cli_collect_is_the_explicit_pipeline_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = []

    async def fake_collect_pipeline() -> None:
        called.append("collect")

    monkeypatch.setattr(cli_app, "collect_pipeline", fake_collect_pipeline)
    cli_app.main(["collect"])

    assert called == ["collect"]


def test_title_normalization_and_clustering() -> None:
    left = "今年第11号台风‘海神’生成"
    right = "第11号台风海神已生成！"
    unrelated = "某品牌发布新款手机"
    assert normalize_title(left) == "今年第11号台风海神生成"
    assert should_merge(left, right)[0]
    assert not should_merge(left, unrelated)[0]
    assert title_similarity(left, right) > title_similarity(left, unrelated)
    assert should_merge("多人因捏造散布涉汛谣言被拘", "多人捏造散布汛情灾情谣言被拘")[0]
    assert should_merge("中国首个禁售燃油车省份确认", "禁售燃油车，海南打响第一枪")[0]
    assert not should_merge("小米发布新款手机", "华为发布新款手机")[0]
    assert not should_merge("北京发布暴雨预警", "上海发布暴雨预警")[0]
    assert not should_merge("苹果公司宣布裁员", "某游戏公司宣布裁员")[0]
    assert not should_merge("张三获得全国冠军", "李四获得全国冠军")[0]
    assert not should_merge(
        "使用iPhone屏幕后视线持续模糊应该怎么办",
        "iPhone20外观设计再次曝光",
    )[0]
    assert not should_merge("YSL推出透明高跟鞋", "具俊晔回应遗产争议")[0]
    assert not should_merge("iPhone16销量创新高", "iPhone20外观设计曝光")[0]


def test_semantic_prototype_retrieval_and_offline_metrics_use_fake_embedder() -> None:
    class FakeEmbedder:
        model_id = "fake-multilingual"
        model_version = "test"

        def encode(self, texts):
            values = []
            for index, _text in enumerate(texts):
                if index == 0 or 1 <= index <= len(POSITIVE_OPPORTUNITY_PROTOTYPES):
                    values.append([1.0, 0.0])
                elif index <= len(POSITIVE_OPPORTUNITY_PROTOTYPES) + len(NEGATIVE_OPPORTUNITY_PROTOTYPES):
                    values.append([0.0, 1.0])
                elif index == 1 + len(POSITIVE_OPPORTUNITY_PROTOTYPES) + len(NEGATIVE_OPPORTUNITY_PROTOTYPES):
                    values.append([1.0, 0.0])
                else:
                    values.append([0.2, 0.8])
            return values

    result = SemanticFeatureExtractor(FakeEmbedder()).extract("小户型需要可折叠实体收纳")
    assert result.positive_similarity == 1.0
    assert result.negative_similarity == 0.0
    assert result.category_matches[0]["category"] == next(iter(CATEGORY_PROTOTYPES))
    assert opportunity_precision_at_k([1, 2, 3], {1, 3}, 2) == 0.5
    assert duplicate_rate([1, 1, 2, 3]) == 0.25


def test_us_amazon_search_term_quality_gate() -> None:
    assert is_search_term_ready("backpack", "US")
    assert is_search_term_ready("clear stadium backpack", "US")
    assert not is_search_term_ready("AI", "US")
    assert not is_search_term_ready("场景化设备配件组合", "US")
    assert pick_search_term("", ["AI", "hiking backpack"], "US") == "hiking backpack"


def test_chinese_amazon_validation_template_headers_are_accepted() -> None:
    fields = [
        "opportunity_id",
        "target_marketplace",
        "provider",
        "search_term",
        "search_volume_360d",
        *MarketScores.model_fields,
        "source",
    ]
    labels = [COLUMN_LABELS[field] for field in fields]
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=labels)
    writer.writeheader()
    writer.writerow(
        {
            COLUMN_LABELS["opportunity_id"]: 7,
            COLUMN_LABELS["target_marketplace"]: "US",
            COLUMN_LABELS["provider"]: "amazon-product-opportunity-explorer",
            COLUMN_LABELS["search_term"]: "backpack",
            COLUMN_LABELS["search_volume_360d"]: "63605600",
            **{COLUMN_LABELS[field]: 4 for field in MarketScores.model_fields},
            COLUMN_LABELS["source"]: "商机探测器下载",
        }
    )
    parsed = parse_validation_csv(stream.getvalue())
    assert parsed[0].opportunity_id == 7
    assert parsed[0].value.metrics["search_volume_360d"] == 63605600
    assert parsed[0].value.query["search_term"] == "backpack"


def amazon_raw_csv_samples() -> tuple[str, str]:
    poe = """按细分市场搜索: backpack,,,,,,,,,,,,,,,,
,,,,,,,,,,,,,,,,
细分市场,热门搜索词 1,热门搜索词 2,热门搜索词 3,搜索量（过去 360 天内）,搜索量增长（过去 180 天）,搜索量（过去 90 天内）,搜索量增长（过去 90 天内）,售出商品数量下限（最近 360 天内）,售出商品数量上限（最近 360 天内）,平均售出商品件数范围下限（最近 360 天内）,平均售出商品件数范围上限（最近 360 天内）,点击量最多的商品数量,平均价格 (USD),最低价格（过去 360 天内）(USD),最高价格（过去 360 天内）(USD),退货率 (过去 360 天)
backpack,backpack,travel backpack,jansport backpack,63605600,'-0.3333,14720276,0.3725,600000,800000,10000,12500,85,36.09,9.31,95.36,0.0464
clear backpack,clear backpack,stadium backpack,backpack for girls,6388097,'-0.3522,1551750,0.617,250000,300000,5000,6000,58,19.79,9.42,48.87,0.04
"""
    hot = """报告范围=["每周"],选择周=["周 28"],搜索词=["backpack"]
搜索频率排名,搜索词,点击量第1的品牌,点击量第2的品牌,点击量第3的品牌,点击量最高的分类,点击量第二的分类,"点击量第 3 的分类",点击量第1的商品：ASIN,"点击量第1的商品: 商品标题",点击量最高的商品：点击份额,点击量第1的商品：转化贡献占比,点击量第2的商品：ASIN,点击量第2的商品：商品标题,点击量第二的商品：点击份额,热门点击商品第2名：转化贡献占比,点击量第三的商品：ASIN,点击量第3的商品：商品标题,点击量第三的商品：点击份额,点击量第3的商品：转化贡献占比,报告日期
19,backpack,JANSPORT,MATEIN,SUPACOOL,Luggage,PC,Home,B06XZTZ7GB,Product One,9.33,5.36,B09G1TPWBQ,Product Two,8.95,0.98,B0D2Q61QF4,Product Three,8.75,2.01,2026-07-11
56,jansport backpack,JANSPORT,LOVEVOOK,Goloni,Luggage,Outdoors,Video Games,B01A6BPAN4,JanSport Product,31.99,7.75,B0007QCQGI,Second,14.79,8.2,B0823VFB4C,Third,7.13,3.19,2026-07-11
"""
    return poe, hot


def test_raw_seller_central_chinese_csv_bundle_is_normalized_and_scored() -> None:
    poe, hot = amazon_raw_csv_samples()
    value = build_raw_amazon_validation(
        opportunity_id=9,
        marketplace="US",
        search_term="backpack",
        product_opportunity_csv=poe,
        hot_search_terms_csv=hot,
    )
    assert value.provider_version == "seller-central-zh-csv-v1"
    assert value.metrics["search_volume_360d"] == 63_605_600
    assert value.metrics["search_volume_growth_180d_pct"] == -33.33
    assert value.metrics["search_frequency_rank"] == 19
    assert value.metrics["top3_click_share_pct"] == 27.03
    assert value.metrics["top3_conversion_share_pct"] == 8.35
    assert value.metrics["source_rows_scanned"] == {
        "商机探测器": 1,
        "品牌分析-热门搜索词": 1,
    }
    assert value.scores.search_demand_score == 5
    assert value.scores.unit_economics_score is None


def test_opportunity_score_is_code_calculated() -> None:
    all_fives = {
        "pain_score": 5,
        "intent_score": 5,
        "segment_score": 5,
        "timing_score": 5,
        "feasibility_score": 5,
        "differentiation_score": 5,
    }
    assert calculate_opportunity_score(all_fives) == 100.0
    assert calculate_opportunity_score({key: 1 for key in all_fives}) == 0.0
    assert calculate_evidence_confidence(8, 4, 0, 1) == 75.0


def test_market_score_does_not_reweight_missing_dimensions() -> None:
    completed = {key: 5 for key in MarketScores.model_fields}
    partial = dict(completed)
    partial["unit_economics_score"] = None
    assert calculate_market_score(completed) == 100.0
    assert calculate_market_score(partial) == 80.0
    final, penalty = calculate_final_score(
        trend_score=80,
        hypothesis_score=80,
        market_score=None,
        validation_status="unavailable",
        risk_level="low",
    )
    assert (final, penalty) == (None, 30.0)
    blocked, _ = calculate_final_score(
        trend_score=100,
        hypothesis_score=100,
        market_score=100,
        validation_status="completed",
        risk_level="blocking",
    )
    assert blocked == 0


def test_unvalidated_hypothesis_never_qualifies_as_recommendation() -> None:
    base = {
        "validation_status": "unavailable",
        "market_score": None,
        "validated_recommendation_score": None,
        "review_status": "approved",
        "risk_level": "low",
    }
    assert not is_validated_recommendation(base)
    assert not is_validated_recommendation(
        {
            **base,
            "validation_status": "partial",
            "market_score": 80.0,
            "validated_recommendation_score": None,
        }
    )
    assert is_validated_recommendation(
        {
            **base,
            "validation_status": "completed",
            "market_score": 80.0,
            "validated_recommendation_score": 78.0,
        }
    )


def test_structured_product_risk_can_block_or_warn() -> None:
    blocking, flags = assess_product_risk(
        {"canonical_title": "明星同款赛事周边"},
        SimpleNamespace(name="官方同款", solution="logo复刻", mvp="", risks=[]),
    )
    assert blocking == "blocking"
    assert {flag["category"] for flag in flags} == {"ip"}
    medium, flags = assess_product_risk(
        {"canonical_title": "高温天气"},
        SimpleNamespace(name="遮阳棚", solution="户外遮阳", mvp="", risks=[]),
    )
    assert medium == "medium"
    assert flags[0]["category"] == "seasonality"


def test_database_schema_and_foreign_keys(tmp_path: Path) -> None:
    db = Database(tmp_path / "nested" / "test.db")
    db.initialize()
    tables = {row["name"] for row in db.all("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "pipeline_runs",
        "source_snapshots",
        "source_items",
        "trend_events",
        "event_members",
        "evidence",
        "analyses",
        "product_opportunities",
        "notification_deliveries",
        "market_validations",
        "opportunity_outcomes",
        "digest_deliveries",
        "semantic_duplicate_candidates",
        "semantic_duplicate_feedback",
        "product_hypotheses",
        "product_hypothesis_feedback",
        "market_evidence",
        "validated_recommendations",
    }.issubset(tables)
    assert {"market", "language", "signal_type"}.issubset(
        {row["name"] for row in db.all("PRAGMA table_info(trend_events)")}
    )
    assert "marketplace" in {
        row["name"] for row in db.all("PRAGMA table_info(product_opportunities)")
    }
    assert {
        "product_keywords_json",
        "risk_level",
        "hypothesis_score",
        "market_score",
        "final_score",
        "validation_status",
    }.issubset(
        {row["name"] for row in db.all("PRAGMA table_info(product_opportunities)")}
    )
    assert db.acquire_lease("pipeline", "owner-a", ttl_seconds=60)
    assert not db.acquire_lease("pipeline", "owner-b", ttl_seconds=60)
    assert db.renew_lease("pipeline", "owner-a", ttl_seconds=120)
    assert not db.renew_lease("pipeline", "owner-b", ttl_seconds=120)
    db.release_lease("pipeline", "owner-a")
    assert db.acquire_lease("pipeline", "owner-b", ttl_seconds=60)


def test_notification_claim_is_atomic(tmp_path: Path) -> None:
    db = Database(tmp_path / "claim.db")
    db.initialize()
    run_id = "run"
    now = "2026-07-13T00:00:00+00:00"
    db.execute(
        "INSERT INTO pipeline_runs(id,trigger,status,stage,started_at,config_json) VALUES(?, 'test', 'completed', 'completed', ?, '{}')",
        (run_id, now),
    )
    event_id = db.execute(
        """INSERT INTO trend_events(canonical_title,normalized_title,first_seen_at,last_seen_at,created_at,updated_at)
        VALUES('event','event',?,?,?,?)""",
        (now, now, now, now),
    )
    analysis_id = db.execute(
        """INSERT INTO analyses(event_id,run_id,engine,model,prompt_version,output_json,created_at)
        VALUES(?,?, 'rules','rules','v1','{}',?)""",
        (event_id, run_id, now),
    )
    opportunity_id = db.execute(
        """INSERT INTO product_opportunities(
        analysis_id,event_id,name,target_segment,scenario,jtbd,pain_points_json,solution,mvp,price_band,
        channels_json,risks_json,pain_score,intent_score,segment_score,timing_score,feasibility_score,
        differentiation_score,opportunity_score,evidence_confidence,created_at,updated_at)
        VALUES(?,?, 'n','s','s','j','[]','x','m','p','[]','[]',1,1,1,1,1,1,0,0,?,?)""",
        (analysis_id, event_id, now, now),
    )
    first, delivery = db.claim_notification(opportunity_id, "key")
    second, same_delivery = db.claim_notification(opportunity_id, "key")
    assert first is True
    assert second is False
    assert delivery["id"] == same_delivery["id"]
    assert same_delivery["status"] == "sending"
    db.execute(
        "UPDATE notification_deliveries SET attempted_at='2020-01-01T00:00:00+00:00' WHERE id=?",
        (delivery["id"],),
    )
    reclaimed, stale_delivery = db.claim_notification(opportunity_id, "key")
    assert reclaimed is False
    assert stale_delivery["status"] == "unknown"


def test_digest_claim_is_atomic_and_marks_stale_send_unknown(tmp_path: Path) -> None:
    db = Database(tmp_path / "digest-claim.db")
    db.initialize()
    first, delivery = db.claim_digest("digest-key", {"cn_top3": [], "overseas_top3": []})
    second, same = db.claim_digest("digest-key", {"cn_top3": [], "overseas_top3": []})
    assert first is True
    assert second is False
    assert delivery["id"] == same["id"]
    assert same["status"] == "sending"
    db.execute(
        "UPDATE digest_deliveries SET attempted_at='2020-01-01T00:00:00+00:00' WHERE id=?",
        (delivery["id"],),
    )
    claimed, stale = db.claim_digest("digest-key", {"cn_top3": [], "overseas_top3": []})
    assert claimed is False
    assert stale["status"] == "unknown"


def test_clear_derived_data_preserves_raw_sources(tmp_path: Path) -> None:
    db = Database(tmp_path / "rebuild.db")
    db.initialize()
    now = "2026-07-13T00:00:00+00:00"
    db.execute(
        "INSERT INTO pipeline_runs(id,trigger,status,stage,started_at,config_json) VALUES('r','test','completed','completed',?,'{}')",
        (now,),
    )
    snapshot_id = db.execute(
        """INSERT INTO source_snapshots(run_id,source,fetched_at,success,latency_ms)
        VALUES('r','weibo',?,1,10)""",
        (now,),
    )
    db.execute(
        """INSERT INTO source_items(snapshot_id,source,external_id,title,normalized_title,url,rank,item_count,fetched_at,raw_json)
        VALUES(?,'weibo','1','title','title','https://example.com',1,1,?,'{}')""",
        (snapshot_id, now),
    )
    db.execute(
        """INSERT INTO trend_events(canonical_title,normalized_title,first_seen_at,last_seen_at,created_at,updated_at)
        VALUES('event','event',?,?,?,?)""",
        (now, now, now, now),
    )
    db.clear_derived_data()
    assert db.one("SELECT COUNT(*) n FROM source_items")["n"] == 1
    assert db.one("SELECT COUNT(*) n FROM trend_events")["n"] == 0


def test_push_api_distinguishes_in_progress_and_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "api.db")
    db.initialize()
    now = "2026-07-13T00:00:00+00:00"
    db.execute(
        "INSERT INTO pipeline_runs(id,trigger,status,stage,started_at,config_json) VALUES('r','test','completed','completed',?,'{}')",
        (now,),
    )
    event_id = db.execute(
        """INSERT INTO trend_events(canonical_title,normalized_title,first_seen_at,last_seen_at,created_at,updated_at)
        VALUES('event','event',?,?,?,?)""",
        (now, now, now, now),
    )
    analysis_id = db.execute(
        """INSERT INTO analyses(event_id,run_id,engine,model,prompt_version,output_json,created_at)
        VALUES(?,'r','rules','rules','v1','{}',?)""",
        (event_id, now),
    )
    opportunity_id = db.execute(
        """INSERT INTO product_opportunities(
        analysis_id,event_id,name,target_segment,scenario,jtbd,pain_points_json,solution,mvp,price_band,
        channels_json,risks_json,pain_score,intent_score,segment_score,timing_score,feasibility_score,
        differentiation_score,opportunity_score,evidence_confidence,review_status,
        risk_level,validation_status,market_score,validated_recommendation_score,
        score_formula_version,
        created_at,updated_at)
        VALUES(?,?,'n','s','s','j','[]','x','m','p','[]','[]',1,1,1,1,1,1,0,0,
        'approved','low','completed',80,80,'opportunity-v2',?,?)""",
        (analysis_id, event_id, now, now),
    )
    destination_hash = hashlib.sha256(b"unconfigured").hexdigest()[:16]
    key = f"feishu:{opportunity_id}:{destination_hash}"
    _, delivery = db.claim_notification(opportunity_id, key)
    monkeypatch.setattr(main_app, "db", db)
    with TestClient(main_app.app) as client:
        response = client.post(f"/api/opportunities/{opportunity_id}/push")
        assert response.status_code == 202
        assert response.json()["status"] == "in_progress"
        db.execute(
            "UPDATE notification_deliveries SET attempted_at='2020-01-01T00:00:00+00:00' WHERE id=?",
            (delivery["id"],),
        )
        response = client.post(f"/api/opportunities/{opportunity_id}/push")
        assert response.status_code == 409
        assert "unknown" in response.json()["detail"]


def test_dashboard_and_api_filter_events_by_market(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "market-api.db")
    db.initialize()
    now = "2026-07-14T00:00:00+00:00"
    for market, title in (("CN", "国内趋势"), ("US", "Overseas trend")):
        db.execute(
            """INSERT INTO trend_events
            (canonical_title,normalized_title,market,first_seen_at,last_seen_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?)""",
            (title, title.casefold(), market, now, now, now, now),
        )
    monkeypatch.setattr(main_app, "db", db)
    with TestClient(main_app.app) as client:
        page = client.get("/?market=US")
        assert page.status_code == 200
        assert "Overseas trend" in page.text
        assert "国内趋势" not in page.text
        response = client.get("/api/events?market=US")
        assert response.status_code == 200
        assert [event["market"] for event in response.json()] == ["US"]
        assert client.get("/api/events?market=US%27--").status_code == 400


def test_digest_has_separate_cn_and_overseas_top_three(tmp_path: Path) -> None:
    db = Database(tmp_path / "digest.db")
    db.initialize()
    now = "2026-07-16T00:00:00+00:00"
    db.execute(
        "INSERT INTO pipeline_runs(id,trigger,status,stage,started_at,config_json) VALUES('r','test','completed','completed',?,'{}')",
        (now,),
    )
    for region_index, market in enumerate(("CN", "US")):
        for index in range(5):
            event_id = db.execute(
                """INSERT INTO trend_events
                (canonical_title,normalized_title,market,trend_score,first_seen_at,last_seen_at,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (f"{market}-{index}", f"{market.lower()}{index}", market, 90-index, now, now, now, now),
            )
            analysis_id = db.execute(
                """INSERT INTO analyses(event_id,run_id,engine,model,prompt_version,output_json,created_at)
                VALUES(?,'r','rules','rules','v2','{}',?)""",
                (event_id, now),
            )
            risk = "blocking" if index == 0 else "low"
            product_name = f"{market} duplicate" if index in {1, 2} else f"{market} product {index}"
            db.execute(
                """INSERT INTO product_opportunities(
                analysis_id,event_id,name,target_segment,scenario,jtbd,pain_points_json,solution,mvp,price_band,
                marketplace,channels_json,risks_json,pain_score,intent_score,segment_score,timing_score,
                feasibility_score,differentiation_score,opportunity_score,evidence_confidence,risk_level,
                validation_status,score_formula_version,created_at,updated_at)
                VALUES(?,?,?,'s','s','j','[]','x','m','p',?,'[]','[]',1,1,1,1,1,1,?,?,?,'unavailable',?,?,?)""",
                (analysis_id, event_id, product_name, market, 99-index, 50, risk, "opportunity-v2", now, now),
            )
    digest = build_daily_digest(db)
    assert len(digest["cn_top3"]) == 3
    assert len(digest["overseas_top3"]) == 3
    assert {item["market"] for item in digest["cn_top3"]} == {"CN"}
    assert {item["market"] for item in digest["overseas_top3"]} == {"US"}
    assert digest["policy"]["content_type"] == "trend_event"
    assert digest["policy"]["not_a_product_recommendation"] is True
    assert [item["event_title"] for item in digest["cn_top3"]] == ["CN-0", "CN-1", "CN-2"]
    assert [item["event_title"] for item in digest["overseas_top3"]] == ["US-0", "US-1", "US-2"]


def test_duplicate_candidates_are_collapsed_and_all_lists_share_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "deduplication.db")
    db.initialize()
    now = "2026-07-17T00:00:00+00:00"
    db.execute(
        "INSERT INTO pipeline_runs(id,trigger,status,stage,started_at,config_json) "
        "VALUES('r','test','completed','completed',?,'{}')",
        (now,),
    )

    def add_opportunity(
        event_title: str,
        name: str,
        score: float,
        target: str = "US",
        validation_status: str = "unavailable",
        review_status: str = "pending",
    ) -> int:
        event_id = db.execute(
            """INSERT INTO trend_events
            (canonical_title,normalized_title,market,trend_score,first_seen_at,last_seen_at,created_at,updated_at)
            VALUES(?,?, 'GLOBAL',80,?,?,?,?)""",
            (event_title, normalized_identity(event_title), now, now, now, now),
        )
        analysis_id = db.execute(
            """INSERT INTO analyses(event_id,run_id,engine,model,prompt_version,output_json,created_at)
            VALUES(?,'r','rules','rules','v2','{}',?)""",
            (event_id, now),
        )
        return db.execute(
            """INSERT INTO product_opportunities(
            analysis_id,event_id,name,target_segment,scenario,jtbd,pain_points_json,solution,mvp,
            price_band,marketplace,target_marketplace,channels_json,risks_json,pain_score,intent_score,
            segment_score,timing_score,feasibility_score,differentiation_score,hypothesis_score,
            opportunity_score,final_score,evidence_confidence,risk_level,validation_status,
            review_status,score_formula_version,created_at,updated_at)
            VALUES(?,?,?,'s','s','j','[]','x','m','$20','Amazon.com',?,'[]','[]',4,4,4,4,4,3,
                   ?,?,?,60,'low',?,?,'opportunity-v2',?,?)""",
            (
                analysis_id,
                event_id,
                name,
                target,
                score,
                score,
                score,
                validation_status,
                review_status,
                now,
                now,
            ),
        )

    lower_id = add_opportunity("Signal A", "新技术 选购包", 40)
    keeper_id = add_opportunity("Signal B", "新技术－选购包", 70)
    gb_id = add_opportunity("Signal C", "新技术选购包", 60, target="GB")
    worked_id = add_opportunity(
        "Signal D", "人工验证产品", 55, validation_status="partial"
    )
    generated_id = add_opportunity("Signal E", "人工验证产品", 95)

    result = collapse_unworked_duplicate_opportunities(db)
    assert set(result["superseded_ids"]) == {lower_id, generated_id}
    assert result["keepers"][lower_id] == keeper_id
    assert result["keepers"][generated_id] == worked_id
    assert db.one("SELECT review_status FROM product_opportunities WHERE id=?", (keeper_id,))[
        "review_status"
    ] == "pending"
    assert db.one("SELECT review_status FROM product_opportunities WHERE id=?", (gb_id,))[
        "review_status"
    ] == "pending"

    active = db.all(
        "SELECT * FROM product_opportunities WHERE review_status!='superseded' ORDER BY id"
    )
    assert len(deduplicate_opportunities(active)) == 3
    assert len(deduplicate_events([
        {"id": 1, "canonical_title": "Same Signal", "market": "US"},
        {"id": 2, "canonical_title": "same-signal", "market": "US"},
        {"id": 3, "canonical_title": "Same Signal", "market": "CN"},
    ])) == 2

    monkeypatch.setattr(main_app, "db", db)
    with TestClient(main_app.app) as client:
        queue = client.get("/api/opportunities/pending-validation?marketplace=US")
        assert queue.status_code == 200
        assert queue.json() == []


def test_market_validation_review_and_outcome_apis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "validation-api.db")
    db.initialize()
    now = "2026-07-16T00:00:00+00:00"
    db.execute(
        "INSERT INTO pipeline_runs(id,trigger,status,stage,started_at,config_json) VALUES('r','test','completed','completed',?,'{}')",
        (now,),
    )
    event_id = db.execute(
        """INSERT INTO trend_events
        (canonical_title,normalized_title,market,trend_score,first_seen_at,last_seen_at,created_at,updated_at)
        VALUES('event','event','US',80,?,?,?,?)""",
        (now, now, now, now),
    )
    analysis_id = db.execute(
        """INSERT INTO analyses(event_id,run_id,engine,model,prompt_version,output_json,created_at)
        VALUES(?,'r','rules','rules','v2','{}',?)""",
        (event_id, now),
    )
    opportunity_id = db.execute(
        """INSERT INTO product_opportunities(
        analysis_id,event_id,name,target_segment,scenario,jtbd,pain_points_json,solution,mvp,price_band,
        channels_json,risks_json,pain_score,intent_score,segment_score,timing_score,feasibility_score,
        differentiation_score,hypothesis_score,opportunity_score,evidence_confidence,created_at,updated_at)
        VALUES(?,?,'n','s','s','j','[]','x','m','p','[]','[]',1,1,1,1,1,1,80,50,40,?,?)""",
        (analysis_id, event_id, now, now),
    )
    monkeypatch.setattr(main_app, "db", db)
    scores = {key: 5 for key in MarketScores.model_fields}
    with TestClient(main_app.app) as client:
        response = client.post(
            f"/api/opportunities/{opportunity_id}/validation",
            json={"provider": "manual", "provider_version": "v1", "scores": scores, "sources": ["SellerSprite export"]},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "completed"
        assert response.json()["market_score"] == 100.0
        review = client.post(
            f"/api/opportunities/{opportunity_id}/review",
            json={"status": "approved", "note": "关键词和毛利已人工复核"},
        )
        assert review.status_code == 200
        outcome = client.post(
            f"/api/opportunities/{opportunity_id}/outcomes/7",
            json={"result": "positive", "metrics": {"sample_orders": 3}, "note": "样品反馈良好"},
        )
        assert outcome.status_code == 200
        detail = client.get(f"/events/{event_id}")
        assert detail.status_code == 200
        assert "已验证推荐分" not in detail.text
        assert "商品方向" not in detail.text
        assert "SellerSprite export" not in detail.text
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "先判断变化" in dashboard.text
        assert "趋势信号 Top 3" not in dashboard.text
    saved = db.one("SELECT * FROM product_opportunities WHERE id=?", (opportunity_id,))
    assert saved["validation_status"] == "completed"
    assert saved["validated_recommendation_score"] is not None
    assert saved["review_status"] == "approved"
    assert saved["reviewer_note"] == "关键词和毛利已人工复核"
    assert db.one(
        "SELECT result FROM opportunity_outcomes WHERE opportunity_id=? AND horizon_days=7",
        (opportunity_id,),
    )["result"] == "positive"
    db.execute(
        "UPDATE product_opportunities SET risk_level='blocking',review_status='pending' WHERE id=?",
        (opportunity_id,),
    )
    with TestClient(main_app.app) as client:
        denied_review = client.post(
            f"/api/opportunities/{opportunity_id}/review",
            json={"status": "approved", "note": "try override"},
        )
        assert denied_review.status_code == 409
        db.execute(
            "UPDATE product_opportunities SET review_status='approved' WHERE id=?",
            (opportunity_id,),
        )
        denied_push = client.post(f"/api/opportunities/{opportunity_id}/push")
        assert denied_push.status_code == 409


def test_pending_queue_csv_import_and_target_marketplace_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "amazon-validation.db")
    db.initialize()
    now = "2026-07-17T00:00:00+00:00"
    db.execute(
        "INSERT INTO pipeline_runs(id,trigger,status,stage,started_at,config_json) VALUES('r','test','completed','completed',?,'{}')",
        (now,),
    )
    event_id = db.execute(
        """INSERT INTO trend_events
        (canonical_title,normalized_title,market,trend_score,first_seen_at,last_seen_at,created_at,updated_at)
        VALUES('中国来源的收纳趋势','trend','CN',82,?,?,?,?)""",
        (now, now, now, now),
    )
    analysis_id = db.execute(
        """INSERT INTO analyses(event_id,run_id,engine,model,prompt_version,output_json,created_at)
        VALUES(?,'r','rules','rules','v2','{}',?)""",
        (event_id, now),
    )
    opportunity_id = db.execute(
        """INSERT INTO product_opportunities(
        analysis_id,event_id,name,product_keywords_json,category,target_segment,scenario,jtbd,
        pain_points_json,solution,mvp,price_band,marketplace,target_marketplace,channels_json,risks_json,
        pain_score,intent_score,segment_score,timing_score,feasibility_score,differentiation_score,
        hypothesis_score,opportunity_score,final_score,evidence_confidence,risk_level,validation_status,
        score_formula_version,created_at,updated_at)
        VALUES(?,?,'折叠收纳架','[\"folding organizer\"]','home','s','s','j','[]','x','m','$19-39',
        'Amazon.com','US','[]','[]',4,4,4,4,4,3,78,40,40,60,'low','unavailable','opportunity-v2',?,?)""",
        (analysis_id, event_id, now, now),
    )
    monkeypatch.setattr(main_app, "db", db)

    headers = [
        "opportunity_id",
        "target_marketplace",
        "provider",
        "collected_at",
        "search_term",
        "search_volume",
        *MarketScores.model_fields,
        "source",
        "note",
    ]
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=headers)
    writer.writeheader()
    writer.writerow(
        {
            "opportunity_id": opportunity_id,
            "target_marketplace": "US",
            "provider": "amazon-product-opportunity-explorer",
            "collected_at": "2026-07-17",
            "search_term": "backpack",
            "search_volume": "12000",
            **{field: 4 for field in MarketScores.model_fields},
            "source": "Seller Central export",
            "note": "首轮一方数据验证",
        }
    )

    with TestClient(main_app.app) as client:
        queue = client.get("/api/opportunities/pending-validation?marketplace=US")
        assert queue.status_code == 200
        assert queue.json() == []
        assert client.post(
            f"/api/opportunities/{opportunity_id}/search-term",
            json={"search_term": "AI"},
        ).status_code == 400
        changed_query = client.post(
            f"/api/opportunities/{opportunity_id}/search-term",
            json={"search_term": "backpack"},
        )
        assert changed_query.status_code == 200
        assert changed_query.json()["query_readiness"] == "ready"
        poe, hot = amazon_raw_csv_samples()
        raw_import = client.post(
            f"/api/opportunities/{opportunity_id}/amazon-raw-import",
            json={
                "product_opportunity_csv": poe,
                "hot_search_terms_csv": hot,
            },
        )
        assert raw_import.status_code == 200
        assert raw_import.json()["status"] == "partial"
        assert raw_import.json()["market_score"] == 56.3
        assert raw_import.json()["source_rows_scanned"]["商机探测器"] == 1
        page = client.get("/validation?marketplace=US")
        assert page.status_code == 200
        assert "当前没有可进入市场验证的商品方向" in page.text
        template = client.get("/api/market-validations/template.csv?marketplace=US")
        assert template.status_code == 200
        assert "backpack" not in template.text

        imported = client.post(
            "/api/market-validations/import",
            content=stream.getvalue().encode("utf-8"),
            headers={"Content-Type": "text/csv"},
        )
        assert imported.status_code == 200
        assert imported.json()["count"] == 1
        assert imported.json()["results"][0]["status"] == "completed"
        assert client.get("/api/opportunities/pending-validation?marketplace=US").json() == []

        changed = client.post(
            f"/api/opportunities/{opportunity_id}/target-marketplace",
            json={"target_marketplace": "GB"},
        )
        assert changed.status_code == 200
        assert changed.json()["marketplace"] == "Amazon.co.uk"

    saved = db.one("SELECT * FROM product_opportunities WHERE id=?", (opportunity_id,))
    assert saved["target_marketplace"] == "GB"
    assert saved["market_score"] is None
    assert saved["validation_status"] == "pending"
    assert saved["review_status"] == "pending"
    latest = db.one(
        "SELECT * FROM market_validations WHERE opportunity_id=? ORDER BY id DESC LIMIT 1",
        (opportunity_id,),
    )
    assert latest["provider_version"] == "target-change-v1"


@pytest.mark.asyncio
async def test_pipeline_stops_at_research_candidate_without_automatic_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "pipeline-v2.db")
    db.initialize()
    now = "2026-07-16T00:00:00+00:00"
    db.execute(
        "INSERT INTO pipeline_runs(id,trigger,status,stage,started_at,config_json) VALUES('r','test','running','analyze',?,'{}')",
        (now,),
    )
    snapshot_id = db.execute(
        """INSERT INTO source_snapshots
        (run_id,source,market,language,signal_type,fetched_at,success,latency_ms)
        VALUES('r','google-trends-us','US','en','search',?,1,10)""",
        (now,),
    )
    item_id = db.execute(
        """INSERT INTO source_items
        (snapshot_id,source,market,language,signal_type,external_id,title,
         normalized_title,url,rank,item_count,fetched_at,raw_json)
        VALUES(?,'google-trends-us','US','en','search','1',?,?,'https://example.com/story',1,10,?,'{}')""",
        (
            snapshot_id,
            "Best storage organizer for small apartments",
            "beststorageorganizerforsmallapartments",
            now,
        ),
    )
    event_id = db.execute(
        """INSERT INTO trend_events
        (canonical_title,normalized_title,market,language,signal_type,trend_score,
         first_seen_at,last_seen_at,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            "Best storage organizer for small apartments",
            "beststorageorganizerforsmallapartments",
            "US", "en", "search", 80, now, now, now, now,
        ),
    )
    db.execute(
        "INSERT INTO event_members(event_id,source_item_id,match_method,match_score) VALUES(?,?,'new',1)",
        (event_id, item_id),
    )

    async def fake_fetch(url: str, title: str) -> EvidenceResult:
        return EvidenceResult(url, title, FULL_ARTICLE_ONE, now, 200, "hash")

    monkeypatch.setattr(pipeline_module, "fetch_evidence", fake_fetch)
    pipeline = Pipeline(db, make_settings(tmp_path))
    await pipeline._build_research_candidate(event_id)
    assert db.all("SELECT * FROM product_opportunities WHERE event_id=?", (event_id,)) == []
    analysis = db.one("SELECT * FROM analyses WHERE event_id=?", (event_id,))
    assert analysis is None
    assert db.all("SELECT * FROM opportunity_signals WHERE event_id=?", (event_id,)) == []
    assert db.all("SELECT * FROM market_validations") == []
    semantic = db.one("SELECT * FROM semantic_event_features WHERE event_id=?", (event_id,))
    assert semantic["status"] == "disabled"
    assert semantic["embedding_json"] is None
    assert "disabled" in semantic["error"]
    bundle = db.one(
        "SELECT * FROM evidence_bundles WHERE event_id=? ORDER BY id DESC LIMIT 1",
        (event_id,),
    )
    assert bundle["full_text_count"] == 1
    assert bundle["title_only_count"] == 0
    assert bundle["readiness_status"] == "partial"
    fallback_candidate = db.one(
        "SELECT * FROM research_candidates WHERE event_id=? AND status='pending'",
        (event_id,),
    )
    assert fallback_candidate is not None
    assert fallback_candidate["semantic_feature_id"] is None
    assert json.loads(fallback_candidate["category_candidates_json"]) == []
    assert fallback_candidate["engine"] == "deterministic-research-rules"
    assert fallback_candidate["version"] == "research-candidate-v2"
    assert "无类目候选" in fallback_candidate["candidate_reason"]
    fallback_candidate_id = fallback_candidate["id"]
    monkeypatch.setattr(main_app, "db", db)
    with TestClient(main_app.app) as client:
        research_page = client.get("/research")
        event_page = client.get(f"/events/{event_id}")
    assert research_page.status_code == 200
    assert event_page.status_code == 200
    assert "打开机会判断" in research_page.text
    assert "已有部分证据" in research_page.text
    assert "ResearchCandidate" not in research_page.text
    assert "进入判断任务" in event_page.text
    assert event_page.text.index("当前进度") < event_page.text.index("已采用的关键证据")
    assert "完成机会判断" not in event_page.text
    assert "来源记录与技术信息" in event_page.text
    assert "人工创建一条可审计的机会线索" not in event_page.text

    class MissingModelExtractor:
        def extract(self, _text):
            raise EmbeddingUnavailable("model is not present in the local cache")

    pipeline.semantic_extractor = MissingModelExtractor()
    await pipeline._persist_semantic_features(
        db.one("SELECT * FROM trend_events WHERE id=?", (event_id,)),
        db.all("SELECT * FROM evidence WHERE event_id=?", (event_id,)),
    )
    semantic = db.one("SELECT * FROM semantic_event_features WHERE event_id=?", (event_id,))
    assert semantic["status"] == "unavailable"
    assert "local cache" in semantic["error"]
    class ReadyModelExtractor:
        def extract(self, _text):
            return SimpleNamespace(
                embedding=[1.0, 0.0],
                category_matches=[{"category": "家居收纳", "similarity": 0.82}],
                positive_similarity=0.76,
                negative_similarity=0.51,
                opportunity_similarity=0.25,
            )

    pipeline.semantic_extractor = ReadyModelExtractor()
    await pipeline._build_research_candidate(event_id)
    candidate = db.one(
        "SELECT * FROM research_candidates WHERE event_id=? AND status='pending'",
        (event_id,),
    )
    assert candidate is not None
    assert json.loads(candidate["category_candidates_json"])[0]["category"] == "家居收纳"
    assert candidate["semantic_feature_id"] is not None
    assert db.one(
        "SELECT status FROM research_candidates WHERE id=?", (fallback_candidate_id,)
    )["status"] == "superseded"
    assert db.all("SELECT * FROM opportunity_signals WHERE event_id=?", (event_id,)) == []


@pytest.mark.asyncio
async def test_pipeline_prefilter_rejects_event_before_page_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "prefilter-reject.db")
    db.initialize()
    now = "2026-07-22T00:00:00+00:00"
    db.execute(
        "INSERT INTO pipeline_runs(id,trigger,status,stage,started_at,config_json) "
        "VALUES('r','test','running','research',?,'{}')",
        (now,),
    )
    snapshot_id = db.execute(
        """INSERT INTO source_snapshots
        (run_id,source,market,language,signal_type,fetched_at,success,latency_ms)
        VALUES('r','news','CN','zh','news',?,1,10)""",
        (now,),
    )
    item_id = db.execute(
        """INSERT INTO source_items
        (snapshot_id,source,market,language,signal_type,external_id,title,
         normalized_title,url,rank,item_count,fetched_at,raw_json)
        VALUES(?,'news','CN','zh','news','1',?,?,'https://sports.example/final',1,1,?,'{}')""",
        (snapshot_id, "世界杯决赛赛果公布", "世界杯决赛赛果公布", now),
    )
    event_id = db.execute(
        """INSERT INTO trend_events
        (canonical_title,normalized_title,market,language,signal_type,trend_score,
         first_seen_at,last_seen_at,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            "世界杯决赛赛果公布",
            "世界杯决赛赛果公布",
            "CN",
            "zh",
            "news",
            99,
            now,
            now,
            now,
            now,
        ),
    )
    db.execute(
        "INSERT INTO event_members(event_id,source_item_id,match_method,match_score) "
        "VALUES(?,?,'new',1)",
        (event_id, item_id),
    )
    fetched_urls: list[str] = []

    async def fail_if_fetched(url: str, _title: str) -> EvidenceResult:
        fetched_urls.append(url)
        raise AssertionError("rejected event must not fetch a public page")

    monkeypatch.setattr(pipeline_module, "fetch_evidence", fail_if_fetched)
    await Pipeline(db, make_settings(tmp_path))._build_research_candidate(event_id)

    screening = db.one(
        "SELECT * FROM research_screenings WHERE event_id=?", (event_id,)
    )
    assert screening is not None
    assert screening["decision"] == "rejected"
    assert "sports_or_match" in json.loads(screening["reason_codes_json"])
    assert fetched_urls == []
    assert db.all("SELECT * FROM evidence_collection_runs WHERE event_id=?", (event_id,)) == []
    assert db.all("SELECT * FROM research_candidates WHERE event_id=?", (event_id,)) == []
    monkeypatch.setattr(main_app, "db", db)
    with TestClient(main_app.app) as client:
        override = client.post(
            f"/api/research-screenings/{screening['id']}/review",
            json={"decision": "collect_limited_evidence"},
        )
    assert override.status_code == 409
    assert db.all("SELECT * FROM research_screening_reviews") == []


@pytest.mark.asyncio
async def test_pipeline_stops_fetching_immediately_after_minimum_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "early-stop.db")
    db.initialize()
    now = "2026-07-22T00:00:00+00:00"
    db.execute(
        "INSERT INTO pipeline_runs(id,trigger,status,stage,started_at,config_json) "
        "VALUES('r','test','running','research',?,'{}')",
        (now,),
    )
    snapshot_id = db.execute(
        """INSERT INTO source_snapshots
        (run_id,source,market,language,signal_type,fetched_at,success,latency_ms)
        VALUES('r','news','US','en','news',?,1,10)""",
        (now,),
    )
    direct_url = "https://first.example.com/story"
    second_url = "https://second.example.org/story"
    unused_url = "https://unused.example.net/story"
    raw = json.dumps(
        {
            "news_urls": [second_url, unused_url],
            "news_titles": ["Independent report", "Should not be fetched"],
        }
    )
    item_id = db.execute(
        """INSERT INTO source_items
        (snapshot_id,source,market,language,signal_type,external_id,title,
         normalized_title,url,rank,item_count,fetched_at,raw_json)
        VALUES(?,'news','US','en','news','1',?,?,?,1,1,?,?)""",
        (
            snapshot_id,
            "Long-term household storage constraints",
            "longtermhouseholdstorageconstraints",
            direct_url,
            now,
            raw,
        ),
    )
    event_id = db.execute(
        """INSERT INTO trend_events
        (canonical_title,normalized_title,market,language,signal_type,trend_score,
         first_seen_at,last_seen_at,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            "Long-term household storage constraints",
            "longtermhouseholdstorageconstraints",
            "US",
            "en",
            "news",
            80,
            now,
            now,
            now,
            now,
        ),
    )
    db.execute(
        "INSERT INTO event_members(event_id,source_item_id,match_method,match_score) "
        "VALUES(?,?,'new',1)",
        (event_id, item_id),
    )
    fetched_urls: list[str] = []

    async def fake_fetch(url: str, title: str) -> EvidenceResult:
        fetched_urls.append(url)
        excerpt = FULL_ARTICLE_ONE if url == direct_url else FULL_ARTICLE_TWO
        return EvidenceResult(url, title, excerpt, now, 200, url)

    monkeypatch.setattr(pipeline_module, "fetch_evidence", fake_fetch)
    await Pipeline(db, make_settings(tmp_path))._build_research_candidate(event_id)

    assert fetched_urls == [direct_url, second_url]
    bundle = db.one(
        "SELECT * FROM evidence_bundles WHERE event_id=? ORDER BY id DESC LIMIT 1",
        (event_id,),
    )
    assert bundle["readiness_status"] == "ready_for_assessment"
    assert bundle["independent_source_count"] == 2
    collection = db.one(
        "SELECT * FROM evidence_collection_runs WHERE event_id=?", (event_id,)
    )
    assert collection["fetch_attempt_count"] == 2
    assert collection["stop_reason"] == "minimum_evidence_reached"
    assert db.one(
        "SELECT * FROM research_candidates WHERE event_id=? AND status='pending'",
        (event_id,),
    ) is not None


def test_semantic_duplicate_candidates_require_review_and_never_auto_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "semantic-duplicates.db")
    db.initialize()
    now = "2026-07-17T00:00:00+00:00"
    event_ids = []
    for title, market, language in (
        ("Reusable rain cover demand rises", "US", "en"),
        ("可重复使用防雨罩需求上升", "CN", "zh"),
    ):
        event_ids.append(
            db.execute(
                """INSERT INTO trend_events
                (canonical_title,normalized_title,market,language,trend_score,
                 first_seen_at,last_seen_at,created_at,updated_at)
                VALUES (?,?,?,?,80,?,?,?,?)""",
                (title, title.casefold(), market, language, now, now, now, now),
            )
        )
    feature_ids = []
    for event_id, input_hash, embedding in (
        (event_ids[0], "a", [1.0, 0.0]),
        (event_ids[1], "b", [0.999, 0.001]),
    ):
        feature_ids.append(
            db.execute(
                """INSERT INTO semantic_event_features
                (event_id,model_id,model_version,input_hash,feature_version,status,
                 embedding_json,category_matches_json,created_at)
                VALUES (?,'fake-e5','rev-1',?,'semantic-test','ready',?,'[]',?)""",
                (event_id, input_hash, db.json(embedding), now),
            )
        )
    assert create_duplicate_candidates(db, feature_ids[1], threshold=0.8) == 1
    candidate = db.one("SELECT * FROM semantic_duplicate_candidates")
    assert candidate["review_status"] == "pending"
    assert db.one("SELECT COUNT(*) n FROM trend_events")["n"] == 2

    monkeypatch.setattr(main_app, "db", db)
    with TestClient(main_app.app) as client:
        page = client.get("/semantic-review")
        assert page.status_code == 200
        assert "相似度不是同一事件概率" in page.text
        reviewed = client.post(
            f"/api/semantic/duplicate-candidates/{candidate['id']}/feedback",
            json={"feedback_type": "related_not_same", "note": "同主题，不同市场事件"},
        )
        assert reviewed.status_code == 200
        assert reviewed.json()["merged"] is False
        evaluation = client.get("/api/semantic/evaluation?k=5")
        assert evaluation.status_code == 200
        assert evaluation.json()["duplicate_reviewed_count"] == 1
        assert evaluation.json()["duplicate_candidate_precision"] == 0.0
    feedback = db.one("SELECT * FROM semantic_duplicate_feedback")
    snapshot = json.loads(feedback["snapshot_json"])
    assert snapshot["candidate"]["model_version"] == "rev-1"
    assert db.one("SELECT COUNT(*) n FROM trend_events")["n"] == 2


def test_new_evidence_chain_only_recommends_after_all_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "evidence-chain.db")
    db.initialize()
    event_id, evidence_ids, candidate_id = make_research_chain(db)
    evidence_id = evidence_ids[0]
    monkeypatch.setattr(main_app, "db", db)
    with TestClient(main_app.app) as client:
        disabled_direct_signal = client.post(
            f"/api/events/{event_id}/opportunity-signals",
            json={"legacy": True},
        )
        assert disabled_direct_signal.status_code == 410
        judgment = client.post(
            f"/api/research-candidates/{candidate_id}/opportunity-judgment",
            json={
                "assessment": {
                    "assessment_status": "worth_following",
                    "change_type": "居住空间约束持续变化",
                    "consumer_relevance": "多来源显示小空间住户反复调整收纳方式。",
                    "durability": "变化跨季节存在，并非一次性热点。",
                    "lead_time_fit": "持续时间覆盖常规实体商品开发和运输周期。",
                    "target_users": ["小空间住户"],
                    "new_scenarios": ["灵活调整食品储物空间"],
                    "unmet_needs": ["固定结构无法随空间调整"],
                    "related_product_categories": ["家居收纳"],
                    "fact_claims": [
                        {"claim": "两个独立来源记录了重复约束。", "evidence_ids": evidence_ids}
                    ],
                    "inferences": [
                        {"claim": "值得进入商品方向验证。", "evidence_ids": evidence_ids}
                    ],
                    "evidence_ids": evidence_ids,
                    "missing_evidence": ["平台需求和竞争数据"],
                },
                "note": "完整主链测试",
            },
        )
        assert judgment.status_code == 200
        assert judgment.json()["opportunity_signal"]["review_status"] == "follow_up"
        signal_id = judgment.json()["opportunity_signal"]["id"]
        hypothesis_payload = {
            "name": "Foldable pantry shelf insert",
            "physical_form": "可折叠钢架与可替换防滑脚垫组成的实体收纳架",
            "target_users": ["small apartment residents"],
            "scenarios": ["adjusting pantry shelf height"],
            "problem": "fixed shelves waste vertical space",
            "expected_difference": "folds flat and publishes a tested load rating",
            "product_keywords": ["foldable pantry shelf"],
            "query_terms": ["foldable pantry shelf organizer"],
            "target_marketplace": "US",
            "evidence_ids": [evidence_id],
        }
        non_physical = dict(hypothesis_payload)
        non_physical["name"] = "Pantry planning software"
        non_physical["physical_form"] = "软件订阅服务"
        blocked_draft = client.post(
            f"/api/opportunity-signals/{signal_id}/product-hypotheses",
            json=non_physical,
        )
        assert blocked_draft.status_code == 200
        blocked_id = blocked_draft.json()["hypothesis_id"]
        assert blocked_draft.json()["risk_level"] == "blocking"
        assert client.post(
            f"/api/product-hypotheses/{blocked_id}/review",
            json={"status": "ready_for_validation", "note": ""},
        ).status_code == 409

        created = client.post(
            f"/api/opportunity-signals/{signal_id}/product-hypotheses",
            json=hypothesis_payload,
        )
        assert created.status_code == 200
        hypothesis_id = created.json()["hypothesis_id"]
        feedback_after_downstream = client.post(
            f"/api/opportunity-signals/{signal_id}/feedback",
            json={"feedback_type": "wrong_category", "note": "尝试改写上游"},
        )
        assert feedback_after_downstream.status_code == 409
        assert client.post(
            f"/api/product-hypotheses/{hypothesis_id}/review",
            json={"status": "ready_for_validation", "note": "实体结构和查询词已确认"},
        ).status_code == 200
        validation_page = client.get("/validation")
        assert validation_page.status_code == 200
        assert "Foldable pantry shelf insert" in validation_page.text
        assert "系统选出的产品机会" not in validation_page.text
        assert "/api/opportunities/" not in validation_page.text

        partial_scores = {
            "search_demand_score": 4, "purchase_intent_score": 4,
            "competition_score": 4, "unit_economics_score": None,
            "differentiation_score": 4, "execution_score": 4,
            "timing_score": 4, "evidence_score": 4,
        }
        partial = client.post(
            f"/api/product-hypotheses/{hypothesis_id}/market-evidence",
            json={
                "provider": "manual", "provider_version": "manual-v1",
                "marketplace": "US", "query": {"term": "foldable pantry shelf organizer"},
                "scores": partial_scores, "metrics": {}, "sources": ["review sheet"],
            },
        )
        assert partial.status_code == 200
        assert partial.json()["recommendation"] is None
        assert db.one("SELECT COUNT(*) n FROM validated_recommendations")["n"] == 0
        assert client.post(
            f"/api/product-hypotheses/{hypothesis_id}/review",
            json={"status": "draft", "note": "已有证据后尝试退回"},
        ).status_code == 409

        completed_scores = {key: None for key in partial_scores}
        completed_scores["unit_economics_score"] = 4
        completed = client.post(
            f"/api/product-hypotheses/{hypothesis_id}/market-evidence",
            json={
                "provider": "amazon-first-party-manual",
                "provider_version": "manual-v1", "marketplace": "US",
                "query": {"term": "foldable pantry shelf organizer"},
                "scores": completed_scores,
                "metrics": {"search_volume_90d": 12000},
                "sources": ["Seller Central export"],
            },
        )
        assert completed.status_code == 200
        assert completed.json()["recommendation"]["recommendation_score"] > 0
        assert completed.json()["market_evidence"]["provider"] == "evidence-composite"
        assert client.get("/recommendations").status_code == 200
        assert "Foldable pantry shelf insert" in client.get("/recommendations").text
        assert client.post(
            f"/api/product-hypotheses/{hypothesis_id}/review",
            json={"status": "rejected", "note": "尝试改写已验证方向"},
        ).status_code == 409
        assert client.post(
            f"/api/product-hypotheses/{hypothesis_id}/market-evidence",
            json={
                "provider": "manual",
                "provider_version": "manual-v1",
                "marketplace": "US",
                "query": {},
                "scores": partial_scores,
                "metrics": {},
                "sources": ["late evidence"],
            },
        ).status_code == 409

    recommendation = db.one("SELECT * FROM validated_recommendations")
    snapshot = json.loads(recommendation["snapshot_json"])
    assert snapshot["event"]["id"] == event_id
    assert snapshot["opportunity_signal"]["id"] == signal_id
    assert snapshot["product_hypothesis"]["id"] == hypothesis_id
    assert snapshot["market_evidence"]["product_hypothesis_id"] == hypothesis_id
    assert db.one("SELECT status FROM product_hypotheses WHERE id=?", (hypothesis_id,))[
        "status"
    ] == "validated"


@pytest.mark.parametrize(
    "title",
    [
        "中国籍女医生在海外遇害",
        "枪击事件造成伤亡",
        "远程解锁BL锁，黑砖概不负责",
        "17天新生儿被宠物狗咬伤脑袋",
        "严打编造传播涉汛等涉灾网络谣言",
        "Victim injured in fatal shooting",
    ],
)
def test_research_candidate_safety_gate_blocks_commercial_research(title: str) -> None:
    assert is_commercial_research_blocked({"canonical_title": title})


def test_dashboard_and_health_report_candidate_pipeline_not_local_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "pipeline-status.db")
    db.initialize()
    monkeypatch.setattr(main_app, "db", db)
    monkeypatch.setattr(main_app, "settings", make_settings(tmp_path))
    with TestClient(main_app.app) as client:
        dashboard = client.get("/")
        health = client.get("/healthz")
    assert dashboard.status_code == 200
    assert "发现真实变化" in dashboard.text
    assert "判断任务" in dashboard.text
    assert "商品方向" not in dashboard.text
    assert "已验证选品" not in dashboard.text
    assert 'class="system-menu"' not in dashboard.text
    assert "Evidence → Candidate" not in dashboard.text
    assert "local-rules-v2" not in dashboard.text
    assert health.status_code == 200
    assert health.json()["pipeline_mode"] == "evidence-bundle-research-candidate"
    assert health.json()["assessment_mode"] == "human-only"
    assert "analysis_engine" not in health.json()
    assert "legacy_latest_analysis" not in health.json()
    assert health.json()["legacy_audit"]["active_pipeline"] is False
    assert health.json()["latest_pipeline_observation"]["selected_count"] == 0
    assert health.json()["journey_metrics"]["pending_screening_reviews"] == 0
    assert health.json()["journey_metrics"]["active_candidates"] == 0


def test_overseas_analysis_quota_is_not_crowded_out(tmp_path: Path) -> None:
    db = Database(tmp_path / "quota.db")
    db.initialize()
    pipeline = Pipeline(
        db,
        replace(
            make_settings(tmp_path),
            research_candidate_top_n=1,
            overseas_research_candidate_top_n=1,
        ),
    )
    now = "2026-07-14T00:00:00+00:00"
    event_ids = []
    for market, score in (("CN", 99), ("CN", 98), ("US", 40), ("DE", 30)):
        event_ids.append(
            db.execute(
                """INSERT INTO trend_events
                (canonical_title,normalized_title,market,trend_score,first_seen_at,last_seen_at,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (f"event-{market}-{score}", f"event{market}{score}", market, score, now, now, now, now),
            )
        )
    selected = pipeline._select_events(event_ids)
    assert len(selected) == 2
    assert {db.one("SELECT market FROM trend_events WHERE id=?", (event_id,))["market"] for event_id in selected} == {"CN", "US"}


def test_event_page_explains_title_only_evidence_and_negative_semantic_delta(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "event-research-view.db")
    db.initialize()
    now = "2026-07-17T00:00:00+00:00"
    event_id = db.execute(
        """INSERT INTO trend_events
        (canonical_title,normalized_title,market,language,signal_type,trend_score,
         first_seen_at,last_seen_at,created_at,updated_at)
        VALUES('沈阳暴雨','沈阳暴雨','CN','zh','news',82,?,?,?,?)""",
        (now, now, now, now),
    )
    for index in range(3):
        db.execute(
            """INSERT INTO evidence
            (event_id,kind,url,title,excerpt,fetched_at,is_consumer_voice,
             valid_for_analysis,error)
            VALUES(?,'hotlist',?,?,?, ?,0,1,'content too short')""",
            (
                event_id,
                f"https://s.weibo.com/weibo?q=rain-{index}",
                "沈阳暴雨",
                "沈阳暴雨",
                now,
            ),
        )
    db.execute(
        """INSERT INTO semantic_event_features
        (event_id,model_id,model_version,input_hash,feature_version,status,
         category_matches_json,positive_similarity,negative_similarity,
         opportunity_similarity,created_at)
        VALUES(?,'fake-e5','rev-1','rain','semantic-test','ready',?,?,?,?,?)""",
        (
            event_id,
            json.dumps(
                [
                    {"category": "出行户外", "similarity": 0.7927},
                    {"category": "个护整理", "similarity": 0.7824},
                    {"category": "汽车配件", "similarity": 0.7725},
                ],
                ensure_ascii=False,
            ),
            0.7487,
            0.7802,
            -0.0316,
            now,
        ),
    )
    db.execute(
        """INSERT INTO semantic_evaluation_labels
        (event_id,label,note,created_at) VALUES(?,'too_short_term','短时天气事件',?)""",
        (event_id, now),
    )
    monkeypatch.setattr(main_app, "db", db)

    with TestClient(main_app.app) as client:
        page = client.get(f"/events/{event_id}")

    assert page.status_code == 200
    assert "证据不足" in page.text
    assert "当前没有可用于判断的正文或可靠摘要" in page.text
    assert "来源记录与技术信息" in page.text
    assert "出行户外" not in page.text
    assert "负向判断略强" not in page.text


def test_event_page_explains_ready_evidence_without_semantic_feature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "ready-event-page.db")
    db.initialize()
    now = "2026-07-17T00:00:00+00:00"
    event_id = db.execute(
        """INSERT INTO trend_events
        (canonical_title,normalized_title,first_seen_at,last_seen_at,created_at,updated_at)
        VALUES('持续居住空间变化','持续居住空间变化',?,?,?,?)""",
        (now, now, now, now),
    )
    for index, (host, excerpt) in enumerate(
        zip(
            ("official.example", "news.example"),
            (FULL_ARTICLE_ONE, FULL_ARTICLE_TWO),
            strict=True,
        ),
        1,
    ):
        db.execute(
            """INSERT INTO evidence
            (event_id,kind,url,title,excerpt,fetched_at,http_status,content_hash,
             evidence_type,fetch_status,source_name,quality_score,valid_for_analysis)
            VALUES(?,'article',?,?,?, ?,200,?,'full_article','ready',?,0.9,1)""",
            (
                event_id,
                f"https://{host}/story",
                f"Complete report {index}",
                excerpt,
                now,
                f"hash-{index}",
                host,
            ),
        )
    monkeypatch.setattr(main_app, "db", db)

    with TestClient(main_app.app) as client:
        page = client.get(f"/events/{event_id}")

    assert page.status_code == 200
    assert "证据已准备，等待生成三级判断卡" in page.text
    assert "已采用的关键证据" in page.text
    assert "语义状态：尚未运行" not in page.text
    assert page.text.index("当前进度") < page.text.index("已采用的关键证据")


def test_evidence_metadata_migration_classifies_legacy_rows(tmp_path: Path) -> None:
    db = Database(tmp_path / "legacy-evidence.db")
    db.initialize()
    now = "2026-07-17T00:00:00+00:00"
    event_id = db.execute(
        """INSERT INTO trend_events
        (canonical_title,normalized_title,first_seen_at,last_seen_at,created_at,updated_at)
        VALUES('legacy','legacy',?,?,?,?)""",
        (now, now, now, now),
    )
    article_id = db.execute(
        """INSERT INTO evidence
        (event_id,kind,url,title,excerpt,fetched_at,http_status,content_hash)
        VALUES(?,'article','https://news.example/story','Story',
        ?,?,200,'article-hash')""",
        (event_id, FULL_ARTICLE_ONE, now),
    )
    title_id = db.execute(
        """INSERT INTO evidence
        (event_id,kind,url,title,excerpt,fetched_at,error)
        VALUES(?,'hotlist','https://hot.example/item','Hot title','Hot title',?,
        'content too short')""",
        (event_id, now),
    )

    db.initialize()

    article = db.one("SELECT * FROM evidence WHERE id=?", (article_id,))
    title = db.one("SELECT * FROM evidence WHERE id=?", (title_id,))
    assert article["evidence_type"] == "full_article"
    assert article["fetch_status"] == "ready"
    assert article["quality_score"] == 0.9
    assert title["evidence_type"] == "title_only"
    assert title["fetch_status"] == "content_too_short"
    assert title["quality_score"] == 0.1
    assert title["valid_for_analysis"] == 0


def test_evidence_quality_and_failure_status_mapping() -> None:
    assert normalize_fetch_status({"error": "content too short"}) == FetchStatus.CONTENT_TOO_SHORT
    assert normalize_fetch_status({"error": "HTTP 403 Forbidden"}) == FetchStatus.ROBOTS_OR_ACCESS_DENIED
    assert normalize_fetch_status({"error": "request timed out"}) == FetchStatus.TIMEOUT
    assert calculate_evidence_quality(
        {
            "kind": "official_notice",
            "evidence_type": EvidenceType.OFFICIAL_NOTICE,
            "fetch_status": "ready",
            "valid_for_analysis": 1,
            "excerpt": "Official notice with enough detail for the evidence bundle.",
        }
    ) == 1.0


def test_evidence_bundle_counts_sources_threshold_and_input_hash() -> None:
    event = {"id": 338, "canonical_title": "沈阳暴雨"}
    title_domains = ("s.weibo.com", "douyin.com", "douyin.com")
    title_only = [
        {
            "id": index,
            "kind": "hotlist",
            "evidence_type": "title_only",
            "fetch_status": "content_too_short",
            "url": f"https://{title_domains[index - 1]}/item/{index}",
            "title": "沈阳暴雨",
            "excerpt": "沈阳暴雨",
            "error": "content too short",
            "is_consumer_voice": 0,
        }
        for index in range(1, 4)
    ]
    insufficient = build_evidence_bundle(event, title_only)
    assert insufficient.readiness_status == "insufficient"
    assert insufficient.full_text_count == 0
    assert insufficient.title_only_count == 3
    assert insufficient.independent_source_count == 0
    assert insufficient.readiness_score == 0.3

    ready_evidence = [
        {
            "id": 10,
            "kind": "official_notice",
            "evidence_type": "official_notice",
            "fetch_status": "ready",
            "url": "https://weather.gov.example/notice",
            "title": "Official notice",
            "excerpt": "Official notice with a complete account of the persistent change.",
            "valid_for_analysis": 1,
            "is_consumer_voice": 0,
        },
        {
            "id": 11,
            "kind": "article",
            "evidence_type": "full_article",
            "fetch_status": "ready",
            "url": "https://news.example/report",
            "title": "Independent report",
            "excerpt": FULL_ARTICLE_TWO,
            "valid_for_analysis": 1,
            "is_consumer_voice": 0,
        },
        {
            "id": 12,
            "kind": "discussion",
            "evidence_type": "consumer_discussion",
            "fetch_status": "ready",
            "url": "https://community.example/thread",
            "title": "Consumer discussion",
            "excerpt": "People discuss recurring wet-backpack and storage problems.",
            "valid_for_analysis": 1,
            "is_consumer_voice": 1,
        },
    ]
    ready = build_evidence_bundle(event, ready_evidence)
    reordered = build_evidence_bundle(event, list(reversed(ready_evidence)))
    changed = build_evidence_bundle(
        event,
        [{**ready_evidence[0], "excerpt": "Changed content"}, *ready_evidence[1:]],
    )
    assert ready.readiness_status == "ready_for_assessment"
    assert ready.full_text_count == 2
    assert ready.official_source_count == 1
    assert ready.consumer_voice_count == 1
    assert ready.independent_source_count == 3
    assert ready.readiness_score == 2.75
    assert ready.input_hash == reordered.input_hash
    assert ready.input_hash != changed.input_hash


def test_evidence_bundle_accepts_reliable_summary_as_second_source() -> None:
    evidence = [
        {
            "id": 1,
            "kind": "article",
            "evidence_type": "full_article",
            "fetch_status": "ready",
            "url": "https://first.example.com/report",
            "title": "Full report",
            "excerpt": FULL_ARTICLE_ONE,
            "valid_for_analysis": 1,
        },
        {
            "id": 2,
            "kind": "article",
            "evidence_type": "article_summary",
            "fetch_status": "ready",
            "url": "https://second.example.org/summary",
            "title": "Independent summary",
            "excerpt": (
                "An independent newsroom confirmed the recurring household constraint "
                "and described the affected users, timing, and source records."
            ),
            "valid_for_analysis": 1,
        },
    ]

    bundle = build_evidence_bundle({"id": 1}, evidence)

    assert bundle.readiness_score < 1.8
    assert bundle.full_text_count == 1
    assert bundle.independent_source_count == 2
    assert bundle.readiness_status == "ready_for_assessment"
    assert all("质量分" not in item for item in bundle.missing_evidence)


def test_product_view_selects_only_two_strong_independent_key_evidence() -> None:
    rows = [
        {
            "id": 1,
            "evidence_type": "full_article",
            "fetch_status": "ready",
            "valid_for_analysis": 1,
            "quality_score": 0.9,
            "url": "https://news.example.com/one",
            "source_name": "news-a",
            "excerpt": FULL_ARTICLE_ONE,
        },
        {
            "id": 2,
            "evidence_type": "full_article",
            "fetch_status": "ready",
            "valid_for_analysis": 1,
            "quality_score": 0.9,
            "url": "https://local.example.com/two",
            "source_name": "news-a-local",
            "excerpt": FULL_ARTICLE_TWO,
        },
        {
            "id": 3,
            "evidence_type": "article_summary",
            "fetch_status": "ready",
            "valid_for_analysis": 1,
            "quality_score": 0.55,
            "url": "https://independent.example.org/three",
            "source_name": "news-b",
            "excerpt": "Independent reporting confirms the recurring consumer constraint.",
        },
        {
            "id": 4,
            "evidence_type": "title_only",
            "fetch_status": "http_error",
            "valid_for_analysis": 0,
            "quality_score": 0,
            "url": "https://failed.example.net/four",
            "source_name": "failed",
            "excerpt": "",
        },
    ]

    selected = select_key_evidence(rows)

    assert [row["id"] for row in selected] == [2, 3]
    assert all(row["valid_for_analysis"] for row in selected)


def test_evidence_bundle_persistence_is_idempotent_and_clear_is_fk_safe(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "bundle.db")
    db.initialize()
    now = "2026-07-17T00:00:00+00:00"
    event_id = db.execute(
        """INSERT INTO trend_events
        (canonical_title,normalized_title,first_seen_at,last_seen_at,created_at,updated_at)
        VALUES('bundle','bundle',?,?,?,?)""",
        (now, now, now, now),
    )
    evidence_id = db.execute(
        """INSERT INTO evidence
        (event_id,kind,url,title,excerpt,fetched_at,evidence_type,fetch_status,
         source_name,quality_score,valid_for_analysis)
        VALUES(?,'article','https://one.example/story','Story',
        'A complete story excerpt with enough evidence.',?,'full_article','ready',
        'one.example',0.9,1)""",
        (event_id, now),
    )
    event = db.one("SELECT * FROM trend_events WHERE id=?", (event_id,))
    rows = db.all("SELECT * FROM evidence WHERE event_id=?", (event_id,))
    bundle = build_evidence_bundle(event, rows)
    first = persist_evidence_bundle(db, bundle)
    second = persist_evidence_bundle(db, bundle)
    assert first["id"] == second["id"]
    assert first["evidence_ids"] == [evidence_id]
    assert db.one("SELECT COUNT(*) n FROM evidence_bundles")["n"] == 1

    db.clear_derived_data()
    assert db.all("PRAGMA foreign_key_check") == []
    assert db.one("SELECT COUNT(*) n FROM evidence_bundles")["n"] == 0


@pytest.mark.parametrize(
    ("semantic_feature", "expected"),
    [
        ({"status": "disabled", "category_matches": []}, "已禁用"),
        ({"status": "unavailable", "category_matches": []}, "模型不可用"),
        ({"status": "ready", "category_matches": []}, "已就绪"),
        (None, "尚未运行"),
    ],
)
def test_event_research_view_handles_semantic_states_and_missing_data(
    semantic_feature: dict | None, expected: str
) -> None:
    view = build_event_research_view(
        {"id": 1, "canonical_title": "test"},
        {
            "readiness_status": "insufficient",
            "readiness_score": 0,
            "full_text_count": 0,
            "title_only_count": 0,
            "independent_source_count": 0,
            "consumer_voice_count": 0,
            "official_source_count": 0,
            "evidence_ids": [],
            "fetch_failure_reasons": [],
            "missing_evidence": [],
        },
        semantic_feature,
        None,
        [],
        None,
    )
    assert view.semantic_status == expected
    assert view.conclusion_code == "no_evidence"
    assert view.human_label == "尚无人工标签"


@pytest.mark.asyncio
async def test_public_page_fetcher_extracts_json_ld_and_follows_safe_redirects() -> None:
    article_body = FULL_ARTICLE_ONE

    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(
                302, headers={"location": "https://news.example/story"}
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            text=f"""<html><head><title>Independent report</title>
            <meta name="description" content="A sourced report about a recurring change.">
            <script type="application/ld+json">{{"@type":"NewsArticle",
            "articleBody":{json.dumps(article_body)}}}</script></head><body></body></html>""",
        )

    result = await fetch_evidence(
        "https://public.example/start",
        "Fallback",
        transport=httpx.MockTransport(handler),
        host_validator=lambda host: host in {"public.example", "news.example"},
    )
    assert result.fetch_status == "ready"
    assert result.evidence_type == "full_article"
    assert result.url == "https://news.example/story"
    assert article_body in result.excerpt
    assert result.raw_metadata["redirect_chain"] == ["https://news.example/story"]


@pytest.mark.asyncio
async def test_public_page_fetcher_blocks_private_redirect_and_labels_login_wall() -> None:
    redirect_transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            302, headers={"location": "http://127.0.0.1/internal"}
        )
    )
    blocked = await fetch_evidence(
        "https://public.example/start",
        "Fallback",
        transport=redirect_transport,
        host_validator=lambda host: host == "public.example",
    )
    assert blocked.fetch_status == "redirect_blocked"
    assert blocked.evidence_type == "title_only"

    login = await fetch_evidence(
        "https://public.example/login",
        "Fallback",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="<html><title>Sign in to continue</title><body>Login required</body></html>",
            )
        ),
        host_validator=lambda _host: True,
    )
    assert login.fetch_status == "login_required"
    assert login.excerpt == ""


@pytest.mark.asyncio
async def test_related_news_collector_consumes_source_item_raw_urls() -> None:
    source_items = [
        {
            "source": "google-trends-us",
            "title": "storage trend",
            "raw_json": json.dumps(
                {
                    "news_urls": ["https://one.example/a", "https://two.example/b"],
                    "news_titles": ["One", "Two"],
                }
            ),
        }
    ]
    assert related_news_targets(source_items) == [
        ("https://one.example/a", "One", "google-trends-us"),
        ("https://two.example/b", "Two", "google-trends-us"),
    ]

    async def fake_fetch(url: str, title: str) -> EvidenceResult:
        return EvidenceResult(
            url,
            title,
            FULL_ARTICLE_ONE,
            "2026-07-17T00:00:00+00:00",
            200,
            hashlib.sha256(url.encode()).hexdigest(),
        )

    collector = RelatedNewsCollector(fetcher=fake_fetch)
    items = await collector.collect(
        {"id": 1, "canonical_title": "storage trend", "source_items": source_items},
        [],
        ResearchBudget(max_fetch_pages=1),
    )
    assert len(items) == 1
    assert items[0].fetch_method == "related-news"
    assert items[0].evidence_type == "full_article"


@pytest.mark.asyncio
async def test_google_news_rss_search_decodes_and_audits_direct_urls() -> None:
    requested = []
    xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss><channel>
      <item><title>沈阳暴雨造成多处积水 - 新华社</title>
        <link>https://news.google.com/rss/articles/one</link>
        <description>沈阳暴雨造成多处积水，相关部门发布处置进展。</description>
        <pubDate>Fri, 17 Jul 2026 10:00:00 GMT</pubDate><source>新华社</source></item>
      <item><title>完全无关的体育消息 - 体育报</title>
        <link>https://news.google.com/rss/articles/two</link>
        <description>球队公布新赛季名单。</description><source>体育报</source></item>
    </channel></rss>"""

    def handler(request):
        requested.append(request)
        return httpx.Response(200, content=xml.encode("utf-8"))

    async def decode(url: str) -> str | None:
        assert url.endswith("/one")
        return "https://local.news.example.cn/story?utm_source=google&ref=rss"

    provider = GoogleNewsRssSearchProvider(
        decoder=decode,
        transport=httpx.MockTransport(handler),
    )
    hits = await provider.search("沈阳暴雨", market="CN", language="zh", limit=3)

    assert len(hits) == 1
    assert hits[0].url == "https://local.news.example.cn/story"
    assert hits[0].source_name == "新华社"
    assert hits[0].provider_url.endswith("/one")
    assert len(hits[0].query_hash) == 64
    assert dict(requested[0].url.params)["gl"] == "CN"
    assert "when:7d" in dict(requested[0].url.params)["q"]


@pytest.mark.asyncio
async def test_public_news_collector_uses_one_result_per_registrable_domain() -> None:
    class FakeProvider:
        name = "fake-search"

        async def search(self, query: str, **_kwargs) -> list[NewsSearchHit]:
            return [
                NewsSearchHit(
                    url="https://a.example.co.uk/one",
                    title=query,
                    snippet="first",
                    source_name="A",
                    published_at=None,
                    provider=self.name,
                    rank=1,
                    query_hash="a" * 64,
                ),
                NewsSearchHit(
                    url="https://b.example.co.uk/two",
                    title=query,
                    snippet="same publisher domain",
                    source_name="B",
                    published_at=None,
                    provider=self.name,
                    rank=2,
                    query_hash="a" * 64,
                ),
                NewsSearchHit(
                    url="https://independent.example.org/three",
                    title=query,
                    snippet="second domain",
                    source_name="C",
                    published_at=None,
                    provider=self.name,
                    rank=3,
                    query_hash="a" * 64,
                ),
            ]

    async def fake_fetch(url: str, title: str) -> EvidenceResult:
        excerpt = FULL_ARTICLE_ONE if "co.uk" in url else FULL_ARTICLE_TWO
        return EvidenceResult(url, title, excerpt, "2026-07-17", 200, url)

    items = await PublicNewsSearchCollector(
        FakeProvider(), fetcher=fake_fetch, max_results=5
    ).collect(
        {"id": 1, "canonical_title": "storage constraints", "market": "GB"},
        [],
        ResearchBudget(max_search_queries=1, max_fetch_pages=3),
    )

    assert [registrable_domain(item.url) for item in items] == [
        "example.co.uk",
        "example.org",
    ]
    assert all(item.fetch_method == "public-news-search" for item in items)
    assert all(item.raw_metadata["search_query_hash"] == "a" * 64 for item in items)


@pytest.mark.asyncio
async def test_fetcher_rejects_signal_pages_and_unrelated_article_content() -> None:
    signal = await fetch_evidence(
        "https://search.bilibili.com/all?keyword=storage",
        "storage constraints",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text=f"<html><title>Search</title><article>{FULL_ARTICLE_ONE}</article></html>",
            )
        ),
        host_validator=lambda _host: True,
    )
    unrelated = await fetch_evidence(
        "https://news.example/unrelated",
        "沈阳暴雨",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text=f"<html><title>Storage research</title><article>{FULL_ARTICLE_ONE}</article></html>",
            )
        ),
        host_validator=lambda _host: True,
    )

    assert signal.fetch_status == "content_too_short"
    assert signal.evidence_type == "title_only"
    assert "search, hot-list" in signal.error
    assert unrelated.fetch_status == "content_irrelevant"
    assert unrelated.evidence_type == "title_only"
    assert "not relevant" in unrelated.error


def test_independent_sources_collapse_subdomains_and_syndicated_copies() -> None:
    rows = [
        {
            "id": 1,
            "kind": "article",
            "evidence_type": "full_article",
            "fetch_status": "ready",
            "url": "https://news.bbc.co.uk/one",
            "excerpt": FULL_ARTICLE_ONE,
            "valid_for_analysis": 1,
        },
        {
            "id": 2,
            "kind": "article",
            "evidence_type": "full_article",
            "fetch_status": "ready",
            "url": "https://local.bbc.co.uk/two",
            "excerpt": FULL_ARTICLE_TWO,
            "valid_for_analysis": 1,
        },
        {
            "id": 3,
            "kind": "article",
            "evidence_type": "full_article",
            "fetch_status": "ready",
            "url": "https://copied.example.net/three",
            "excerpt": FULL_ARTICLE_ONE,
            "valid_for_analysis": 1,
        },
    ]

    bundle = build_evidence_bundle({"id": 1}, rows)
    assert registrable_domain(rows[0]["url"]) == "bbc.co.uk"
    assert registrable_domain(rows[1]["url"]) == "bbc.co.uk"
    assert bundle.full_text_count == 3
    assert bundle.independent_source_count == 1
    assert bundle.readiness_status == "partial"
    assert canonicalize_public_url("ftp://example.com/file") == ""
    assert canonicalize_public_url("https://user:secret@example.com/file") == ""
    assert canonicalize_public_url("https://example.com:bad/file") == ""


def test_manual_evidence_api_is_idempotent_rebuilds_bundle_and_blocks_private_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "manual-evidence.db")
    db.initialize()
    now = "2026-07-17T00:00:00+00:00"
    event_id = db.execute(
        """INSERT INTO trend_events
        (canonical_title,normalized_title,first_seen_at,last_seen_at,created_at,updated_at)
        VALUES('manual evidence','manualevidence',?,?,?,?)""",
        (now, now, now, now),
    )
    monkeypatch.setattr(main_app, "db", db)
    payload = {
        "evidence_type": "full_article",
        "source_name": "manual interview notes",
        "url": "",
        "title": "Recurring storage constraints",
        "excerpt": FULL_ARTICLE_ONE,
        "is_consumer_voice": True,
        "note": "User supplied text",
    }
    with TestClient(main_app.app) as client:
        first = client.post(f"/api/events/{event_id}/evidence/manual", json=payload)
        second = client.post(f"/api/events/{event_id}/evidence/manual", json=payload)
        evidence = client.get(f"/api/events/{event_id}/evidence")
        bundles = client.get(f"/api/events/{event_id}/evidence-bundles")
        blocked = client.post(
            f"/api/events/{event_id}/evidence/manual",
            json={**payload, "url": "http://127.0.0.1/private"},
        )
        credential_url = client.post(
            f"/api/events/{event_id}/evidence/manual",
            json={**payload, "url": "https://news.example/story?token=do-not-store"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["evidence"]["id"] == second.json()["evidence"]["id"]
    assert len(evidence.json()) == 1
    assert evidence.json()[0]["raw_metadata"]["note"] == "User supplied text"
    assert len(bundles.json()) == 1
    assert bundles.json()[0]["full_text_count"] == 1
    assert blocked.status_code == 400
    assert credential_url.status_code == 400


@pytest.mark.parametrize(
    ("title", "expected", "reason"),
    [
        ("重庆山体崩塌已搜救出8名被困者", "rejected", "disaster_or_casualty"),
        ("世界杯决赛赛果公布", "rejected", "sports_or_match"),
        ("GitHub Copilot developer SDK released", "rejected", "software_or_digital"),
        (
            "Long-term household storage constraints in small apartments",
            "eligible",
            "physical_consumption_link_found",
        ),
        ("A viral topic with no described user or physical scenario", "needs_review", "physical_consumption_link_unclear"),
    ],
)
def test_research_screening_is_explainable_before_fetch(
    title: str, expected: str, reason: str
) -> None:
    decision = screen_research_event(
        {"id": 1, "canonical_title": title, "market": "GLOBAL"}
    )
    assert decision.decision == expected
    assert reason in decision.reason_codes
    assert decision.explanation
    assert len(decision.input_hash) == 64


def test_screening_review_reject_is_visible_audited_and_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "screening-reject.db")
    db.initialize()
    now = "2026-07-22T00:00:00+00:00"
    event_id = db.execute(
        """INSERT INTO trend_events
        (canonical_title,normalized_title,market,language,signal_type,trend_score,
         first_seen_at,last_seen_at,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            "A viral topic with no described user or physical scenario",
            "a viral topic with no described user or physical scenario",
            "US",
            "en",
            "news",
            70,
            now,
            now,
            now,
            now,
        ),
    )
    event = db.one("SELECT * FROM trend_events WHERE id=?", (event_id,))
    screening = persist_research_screening(db, screen_research_event(event))

    class NoCollectionPipeline:
        async def collect_reviewed_screening(self, _screening_id: int):
            raise AssertionError("reject must not start evidence collection")

    monkeypatch.setattr(main_app, "db", db)
    monkeypatch.setattr(main_app, "settings", make_settings(tmp_path))
    monkeypatch.setattr(main_app, "pipeline", NoCollectionPipeline())
    with TestClient(main_app.app) as client:
        page = client.get("/research")
        rejected = client.post(
            f"/api/research-screenings/{screening['id']}/review",
            json={"decision": "reject", "note": "No stable physical scenario."},
        )
        repeated = client.post(
            f"/api/research-screenings/{screening['id']}/review",
            json={"decision": "reject", "note": "A different retry note is ignored."},
        )
        conflict = client.post(
            f"/api/research-screenings/{screening['id']}/review",
            json={"decision": "collect_limited_evidence"},
        )
        cleared_page = client.get("/research")
        event_page = client.get(f"/events/{event_id}")

    assert page.status_code == 200
    assert "初筛待复核" in page.text
    assert "允许有限补证" in page.text
    assert rejected.status_code == repeated.status_code == 200
    assert rejected.json()["collection"] is None
    assert conflict.status_code == 409
    reviews = db.all("SELECT * FROM research_screening_reviews")
    assert len(reviews) == 1
    assert reviews[0]["decision"] == "reject"
    assert reviews[0]["note"] == "No stable physical scenario."
    assert db.all("SELECT * FROM evidence_collection_runs") == []
    assert db.all("SELECT * FROM research_candidates") == []
    assert db.all("SELECT * FROM opportunity_signals") == []
    assert "初筛待复核" not in cleared_page.text
    assert "已人工排除" in event_page.text
    assert "No stable physical scenario." in event_page.text


@pytest.mark.asyncio
async def test_screening_review_approval_runs_one_limited_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "screening-approve.db")
    db.initialize()
    now = "2026-07-22T00:00:00+00:00"
    db.execute(
        "INSERT INTO pipeline_runs(id,trigger,status,stage,started_at,config_json) "
        "VALUES('r','test','running','research',?,'{}')",
        (now,),
    )
    snapshot_id = db.execute(
        """INSERT INTO source_snapshots
        (run_id,source,market,language,signal_type,fetched_at,success,latency_ms)
        VALUES('r','news','US','en','news',?,1,10)""",
        (now,),
    )
    direct_url = "https://first.example.com/ambiguous-story"
    second_url = "https://second.example.org/independent-story"
    raw = json.dumps(
        {"news_urls": [second_url], "news_titles": ["Independent report"]}
    )
    title = "A viral topic with no described user or physical scenario"
    item_id = db.execute(
        """INSERT INTO source_items
        (snapshot_id,source,market,language,signal_type,external_id,title,
         normalized_title,url,rank,item_count,fetched_at,raw_json)
        VALUES(?,'news','US','en','news','1',?,?,?,1,1,?,?)""",
        (snapshot_id, title, title.casefold(), direct_url, now, raw),
    )
    event_id = db.execute(
        """INSERT INTO trend_events
        (canonical_title,normalized_title,market,language,signal_type,trend_score,
         first_seen_at,last_seen_at,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (title, title.casefold(), "US", "en", "news", 80, now, now, now, now),
    )
    db.execute(
        "INSERT INTO event_members(event_id,source_item_id,match_method,match_score) "
        "VALUES(?,?,'new',1)",
        (event_id, item_id),
    )
    fetched_urls: list[str] = []

    async def fake_fetch(url: str, item_title: str) -> EvidenceResult:
        fetched_urls.append(url)
        excerpt = FULL_ARTICLE_ONE if url == direct_url else FULL_ARTICLE_TWO
        return EvidenceResult(url, item_title, excerpt, now, 200, url)

    monkeypatch.setattr(pipeline_module, "fetch_evidence", fake_fetch)
    worker = Pipeline(db, make_settings(tmp_path))
    initial = await worker._build_research_candidate(event_id)
    assert initial["status"] == "screened_out"
    screening_id = int(initial["screening_id"])
    assert fetched_urls == []

    monkeypatch.setattr(main_app, "db", db)
    monkeypatch.setattr(main_app, "settings", make_settings(tmp_path))
    monkeypatch.setattr(main_app, "pipeline", worker)
    with TestClient(main_app.app) as client:
        approved = client.post(
            f"/api/research-screenings/{screening_id}/review",
            json={
                "decision": "collect_limited_evidence",
                "note": "Verify whether the article reveals a durable physical scenario.",
            },
        )
        repeated = client.post(
            f"/api/research-screenings/{screening_id}/review",
            json={"decision": "collect_limited_evidence"},
        )
        conflict = client.post(
            f"/api/research-screenings/{screening_id}/review",
            json={"decision": "reject"},
        )

    assert approved.status_code == repeated.status_code == 200
    assert approved.json()["collection"]["stop_reason"] == "minimum_evidence_reached"
    assert approved.json()["candidate"] is not None
    assert conflict.status_code == 409
    assert fetched_urls == [direct_url, second_url]
    assert db.one("SELECT COUNT(*) n FROM research_screening_reviews")["n"] == 1
    assert db.one("SELECT COUNT(*) n FROM evidence_collection_runs")["n"] == 1
    assert db.one(
        "SELECT COUNT(*) n FROM research_candidates WHERE event_id=? AND status='pending'",
        (event_id,),
    )["n"] == 1
    assert db.one("SELECT COUNT(*) n FROM opportunity_signals")["n"] == 0


def test_pending_candidate_rescreen_removes_legacy_out_of_scope_queue_items(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "candidate-rescreen.db")
    db.initialize()
    eligible_event, _eligible_evidence, eligible_candidate = make_research_chain(db)
    blocked_event, _blocked_evidence, blocked_candidate = make_research_chain(db)
    db.execute(
        "UPDATE trend_events SET canonical_title=? WHERE id=?",
        ("Long-term household storage constraints", eligible_event),
    )
    db.execute(
        "UPDATE trend_events SET canonical_title=? WHERE id=?",
        ("父亲在家长群公开夫妻矛盾被认定家暴", blocked_event),
    )

    result = rescreen_pending_research_candidates(db)

    assert result == {"checked": 2, "superseded": 1}
    assert db.one(
        "SELECT status FROM research_candidates WHERE id=?", (eligible_candidate,)
    )["status"] == "pending"
    assert db.one(
        "SELECT status FROM research_candidates WHERE id=?", (blocked_candidate,)
    )["status"] == "superseded"
    screening = db.one(
        "SELECT * FROM research_screenings WHERE event_id=? ORDER BY id DESC LIMIT 1",
        (blocked_event,),
    )
    assert "crime_or_harm" in json.loads(screening["reason_codes_json"])
    assert eligible_event != blocked_event


def test_research_candidate_keeps_research_scope_and_blocks_sensitive_events() -> None:
    bundle = {
        "id": 5,
        "readiness_status": "insufficient",
        "missing_evidence": ["至少需要 1 条完整正文或官方公告"],
    }
    semantic = {
        "id": 9,
        "status": "ready",
        "category_matches_json": json.dumps(
            [{"category": "出行户外", "similarity": 0.7927}], ensure_ascii=False
        ),
        "positive_similarity": 0.7487,
        "negative_similarity": 0.7802,
        "opportunity_similarity": -0.0316,
    }
    candidate = candidate_from_event(
        {
            "id": 338,
            "canonical_title": "极端降雨频率持续上升影响户外通勤",
            "trend_score": 71.4,
        },
        bundle,
        semantic,
    )
    assert candidate is not None
    assert candidate.category_candidates == [
        {"category": "出行户外", "similarity": 0.7927}
    ]
    dumped = candidate.model_dump_json()
    assert "Amazon" not in dumped and "售价" not in dumped and "商品名" not in dumped
    assert candidate.missing_evidence == bundle["missing_evidence"]

    one_off_weather = candidate_from_event(
        {"id": 342, "canonical_title": "沈阳暴雨", "trend_score": 92},
        {**bundle, "readiness_status": "partial"},
        semantic,
    )
    assert one_off_weather is None

    fallback_bundle = {**bundle, "readiness_status": "partial"}
    without_embedding = candidate_from_event(
        {"id": 339, "canonical_title": "持续居住空间变化", "trend_score": 70},
        fallback_bundle,
        {"id": 10, "status": "disabled", "category_matches_json": "[]"},
    )
    assert without_embedding is not None
    assert without_embedding.semantic_feature_id is None
    assert without_embedding.category_candidates == []
    assert without_embedding.positive_similarity is None
    assert without_embedding.engine == "deterministic-research-rules"
    assert "可核查的实体商品类目关联证据" in without_embedding.missing_evidence
    assert "该事件是否与任何低风险实体消费品类目存在可核查关联？" in (
        without_embedding.research_questions
    )
    fallback_dump = without_embedding.model_dump_json()
    assert (
        "Amazon" not in fallback_dump
        and "售价" not in fallback_dump
        and "商品名" not in fallback_dump
    )

    without_feature = candidate_from_event(
        {"id": 340, "canonical_title": "长期消费者场景变化", "trend_score": 68},
        fallback_bundle,
        None,
    )
    assert without_feature is not None
    assert without_feature.semantic_feature_id is None

    title_only_without_embedding = candidate_from_event(
        {"id": 341, "canonical_title": "只有热榜标题", "trend_score": 90},
        bundle,
        {"id": 11, "status": "disabled", "category_matches_json": "[]"},
    )
    assert title_only_without_embedding is None

    blocked = candidate_from_event(
        {"id": 2, "canonical_title": "枪击事件造成伤亡", "trend_score": 99},
        fallback_bundle,
        None,
    )
    assert blocked is None

    disaster = candidate_from_event(
        {"id": 3, "canonical_title": "重庆彭水山体垮塌", "trend_score": 95},
        fallback_bundle,
        None,
    )
    assert disaster is None

    political_discipline = candidate_from_event(
        {"id": 4, "canonical_title": "某官员被双开", "trend_score": 94},
        fallback_bundle,
        None,
    )
    assert political_discipline is None

    crime = candidate_from_event(
        {"id": 5, "canonical_title": "住户遭遇入室盗窃", "trend_score": 93},
        fallback_bundle,
        None,
    )
    assert crime is None


def test_research_candidate_version_and_bundle_change_supersede_previous(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "candidate-version.db")
    db.initialize()
    now = "2026-07-17T00:00:00+00:00"
    event_id = db.execute(
        """INSERT INTO trend_events
        (canonical_title,normalized_title,trend_score,first_seen_at,last_seen_at,created_at,updated_at)
        VALUES('candidate','candidate',80,?,?,?,?)""",
        (now, now, now, now),
    )
    feature_id = db.execute(
        """INSERT INTO semantic_event_features
        (event_id,model_id,model_version,input_hash,feature_version,status,
         category_matches_json,created_at)
        VALUES(?,'fake','v1','hash','semantic-v1','ready','[]',?)""",
        (event_id, now),
    )
    bundle_ids = []
    for suffix in ("a", "b"):
        bundle_ids.append(
            db.execute(
                """INSERT INTO evidence_bundles
                (event_id,input_hash,version,readiness_status,readiness_score,
                 full_text_count,title_only_count,independent_source_count,
                 consumer_voice_count,official_source_count,created_at)
                VALUES(?,?,'evidence-bundle-v1','insufficient',0.1,0,1,1,0,0,?)""",
                (event_id, suffix, now),
            )
        )
    event = db.one("SELECT * FROM trend_events WHERE id=?", (event_id,))
    semantic = {
        "id": feature_id,
        "status": "ready",
        "category_matches": [{"category": "家居收纳", "similarity": 0.8}],
    }
    first_draft = candidate_from_event(
        event,
        {"id": bundle_ids[0], "readiness_status": "insufficient", "missing_evidence": []},
        semantic,
        version="candidate-v1",
    )
    first = persist_research_candidate(db, first_draft)
    same = persist_research_candidate(db, first_draft)
    second_draft = candidate_from_event(
        event,
        {"id": bundle_ids[1], "readiness_status": "partial", "missing_evidence": []},
        semantic,
        version="candidate-v2",
    )
    second = persist_research_candidate(db, second_draft)
    assert first["id"] == same["id"]
    assert second["id"] != first["id"]
    assert db.one("SELECT status FROM research_candidates WHERE id=?", (first["id"],))[
        "status"
    ] == "superseded"


def test_research_candidate_api_cannot_bypass_screening_and_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "candidate-api.db")
    db.initialize()
    now = "2026-07-17T00:00:00+00:00"
    event_id = db.execute(
        """INSERT INTO trend_events
        (canonical_title,normalized_title,trend_score,first_seen_at,last_seen_at,created_at,updated_at)
        VALUES('持续防雨场景','持续防雨场景',75,?,?,?,?)""",
        (now, now, now, now),
    )
    db.execute(
        """INSERT INTO evidence
        (event_id,kind,url,title,excerpt,fetched_at,evidence_type,fetch_status,
         source_name,quality_score,valid_for_analysis)
        VALUES(?,'hotlist','https://hot.example/rain','持续防雨场景','持续防雨场景',?,
        'title_only','ready','hot',0.1,0)""",
        (event_id, now),
    )
    db.execute(
        """INSERT INTO semantic_event_features
        (event_id,model_id,model_version,input_hash,feature_version,status,
         category_matches_json,positive_similarity,negative_similarity,
         opportunity_similarity,created_at)
        VALUES(?,'fake','v1','hash','semantic-v1','ready',?,0.7,0.6,0.1,?)""",
        (
            event_id,
            json.dumps([{"category": "出行户外", "similarity": 0.79}], ensure_ascii=False),
            now,
        ),
    )
    monkeypatch.setattr(main_app, "db", db)
    with TestClient(main_app.app) as client:
        created = client.post(f"/api/events/{event_id}/research-candidates")
        listing = client.get("/api/research-candidates?status=pending")
        page = client.get("/research")
    assert created.status_code == 409
    assert "必须先通过初筛" in created.json()["detail"]
    assert listing.json() == []
    assert page.status_code == 200
    assert db.one("SELECT COUNT(*) n FROM opportunity_signals")["n"] == 0


def test_research_candidate_status_api_rejects_unproven_transitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "candidate-transitions.db")
    db.initialize()
    _event_id, _evidence_ids, candidate_id = make_research_chain(db)
    monkeypatch.setattr(main_app, "db", db)

    with TestClient(main_app.app) as client:
        completed = client.post(
            f"/api/research-candidates/{candidate_id}/status",
            json={"status": "completed"},
        )
        evidence_ready = client.post(
            f"/api/research-candidates/{candidate_id}/status",
            json={"status": "evidence_ready"},
        )

    assert completed.status_code == 409
    assert evidence_ready.status_code == 409
    assert db.one(
        "SELECT status FROM research_candidates WHERE id=?", (candidate_id,)
    )["status"] == "pending"


def make_research_chain(db: Database, *, ready: bool = True) -> tuple[int, list[int], int]:
    now = "2026-07-17T00:00:00+00:00"
    event_id = db.execute(
        """INSERT INTO trend_events
        (canonical_title,normalized_title,trend_score,first_seen_at,last_seen_at,created_at,updated_at)
        VALUES('Recurring small-space constraints','recurringsmallspace',82,?,?,?,?)""",
        (now, now, now, now),
    )
    evidence_ids = []
    sources = ("official.example", "news.example") if ready else ("hot.example",)
    for index, host in enumerate(sources, 1):
        if ready:
            values = (
                "article",
                "full_article",
                (FULL_ARTICLE_ONE, FULL_ARTICLE_TWO)[index - 1],
                0.9,
                1,
            )
        else:
            values = ("hotlist", "title_only", "Recurring small-space constraints", 0.1, 0)
        evidence_ids.append(
            db.execute(
                """INSERT INTO evidence
                (event_id,kind,url,title,excerpt,fetched_at,evidence_type,fetch_status,
                 source_name,quality_score,valid_for_analysis)
                VALUES(?,?,?,?,?, ?,?,'ready',?,?,?)""",
                (
                    event_id,
                    values[0],
                    f"https://{host}/story-{index}",
                    f"Evidence {index}",
                    values[2],
                    now,
                    values[1],
                    host,
                    values[3],
                    values[4],
                ),
            )
        )
    event = db.one("SELECT * FROM trend_events WHERE id=?", (event_id,))
    bundle = persist_evidence_bundle(
        db,
        build_evidence_bundle(
            event, db.all("SELECT * FROM evidence WHERE event_id=?", (event_id,))
        ),
    )
    feature_id = db.execute(
        """INSERT INTO semantic_event_features
        (event_id,model_id,model_version,input_hash,feature_version,status,
         category_matches_json,positive_similarity,negative_similarity,
         opportunity_similarity,created_at)
        VALUES(?,'fake','v1',?,'semantic-v1','ready',?,0.8,0.5,0.3,?)""",
        (
            event_id,
            f"hash-{event_id}",
            json.dumps([{"category": "家居收纳", "similarity": 0.84}], ensure_ascii=False),
            now,
        ),
    )
    semantic = db.one("SELECT * FROM semantic_event_features WHERE id=?", (feature_id,))
    candidate = persist_research_candidate(
        db,
        candidate_from_event(event, bundle, semantic),
    )
    return event_id, evidence_ids, candidate["id"]


def test_legacy_signal_without_approved_assessment_cannot_create_product_direction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "legacy-signal-chain.db")
    db.initialize()
    event_id, evidence_ids, _candidate_id = make_research_chain(db)
    now = "2026-07-17T00:00:00+00:00"
    db.execute(
        """INSERT INTO pipeline_runs(id,trigger,status,stage,started_at,finished_at,config_json)
        VALUES('legacy-signal-run','test','completed','completed',?,?,'{}')""",
        (now, now),
    )
    analysis_id = db.execute(
        """INSERT INTO analyses
        (event_id,run_id,engine,model,prompt_version,output_json,status,created_at)
        VALUES(?,'legacy-signal-run','human','legacy','legacy','{}','succeeded',?)""",
        (event_id, now),
    )
    signal_id = db.execute(
        """INSERT INTO opportunity_signals
        (event_id,analysis_id,change_type,consumer_relevance_score,
         product_opportunity_score,target_users_json,new_scenarios_json,
         unmet_needs_json,related_product_categories_json,durability,lead_time_fit,
         evidence_ids_json,confidence,missing_evidence_json,review_status,
         engine,model,version,created_at,updated_at)
        VALUES (?,?,?,0,0,?,?,?,?,?,?,?,0,'[]','follow_up','human','legacy','legacy',?,?)""",
        (
            event_id,
            analysis_id,
            "legacy signal",
            db.json(["small-space residents"]),
            db.json(["flexible storage"]),
            db.json(["fixed shelves"]),
            db.json(["storage"]),
            "recurring",
            "fits delivery",
            db.json(evidence_ids),
            now,
            now,
        ),
    )
    monkeypatch.setattr(main_app, "db", db)

    with TestClient(main_app.app) as client:
        response = client.post(
            f"/api/opportunity-signals/{signal_id}/product-hypotheses",
            json={
                "name": "Foldable shelf",
                "physical_form": "foldable steel shelf",
                "target_users": ["small-space residents"],
                "scenarios": ["flexible storage"],
                "problem": "fixed shelves waste space",
                "expected_difference": "foldable adjustable structure",
                "product_keywords": ["foldable shelf"],
                "query_terms": ["foldable pantry shelf"],
                "target_marketplace": "US",
                "evidence_ids": evidence_ids,
            },
        )

    assert response.status_code == 409
    assert db.one("SELECT COUNT(*) n FROM product_hypotheses")["n"] == 0


def test_research_run_is_idempotent_and_completes_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "research-run.db")
    db.initialize()
    _event_id, _evidence_ids, candidate_id = make_research_chain(db)
    monkeypatch.setattr(main_app, "db", db)
    payload = {
        "executor_type": "human",
        "executor_name": "researcher",
        "budget": {"max_search_queries": 2, "max_fetch_pages": 3, "timeout_seconds": 60},
    }
    with TestClient(main_app.app) as client:
        first = client.post(f"/api/research-candidates/{candidate_id}/runs", json=payload)
        second = client.post(f"/api/research-candidates/{candidate_id}/runs", json=payload)
        leased = client.post(
            f"/api/research-candidates/{candidate_id}/runs",
            json={**payload, "executor_name": "other-researcher"},
        )
        completed = client.post(
            f"/api/research-runs/{first.json()['id']}/complete",
            json={"status": "completed"},
        )
    assert first.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert leased.status_code == 409
    assert "active executor" in leased.json()["detail"]
    assert completed.json()["status"] == "completed"
    assert db.one("SELECT status FROM research_candidates WHERE id=?", (candidate_id,))[
        "status"
    ] == "evidence_ready"


def test_browser_research_budget_is_explicitly_unavailable() -> None:
    assert ResearchBudget().max_browser_pages == 0
    with pytest.raises(ValueError):
        ResearchBudget(max_browser_pages=1)


def test_research_run_keeps_insufficient_bundle_state_and_event_page_shows_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "research-run-page.db")
    db.initialize()
    event_id, evidence_ids, candidate_id = make_research_chain(db, ready=False)
    candidate = db.one("SELECT * FROM research_candidates WHERE id=?", (candidate_id,))
    run = start_research_run(
        db,
        candidate,
        ResearchRunInput(
            executor_type="agent",
            executor_name="audited-agent",
            budget=ResearchBudget(max_fetch_pages=2, timeout_seconds=45),
        ),
    )
    record_research_tool_call(
        db,
        run,
        ResearchToolResultInput(
            tool_name="get_context",
            request={"event_id": event_id},
            status="completed",
            result_evidence_ids=evidence_ids,
            latency_ms=7,
        ),
    )
    completed = complete_research_run(
        db,
        run,
        ResearchRunCompleteInput(status="completed"),
    )
    assert completed["status"] == "completed"
    assert db.one("SELECT status FROM research_candidates WHERE id=?", (candidate_id,))[
        "status"
    ] == "insufficient_evidence"

    monkeypatch.setattr(main_app, "db", db)
    with TestClient(main_app.app) as client:
        audited_page = client.get(f"/workbench/items/{candidate_id}")
    assert audited_page.status_code == 200
    assert "技术与审计信息" in audited_page.text
    assert "EvidenceBundle" in audited_page.text
    assert "audited-agent" in audited_page.text

    failed_run = start_research_run(
        db,
        db.one("SELECT * FROM research_candidates WHERE id=?", (candidate_id,)),
        ResearchRunInput(executor_type="agent", executor_name="failed-agent"),
    )
    complete_research_run(
        db,
        failed_run,
        ResearchRunCompleteInput(status="failed", error="provider unavailable"),
    )
    with TestClient(main_app.app) as client:
        page = client.get(f"/workbench/items/{candidate_id}")
    assert page.status_code == 200
    assert "failed-agent" in page.text
    assert "provider unavailable" in page.text
    assert "provider unavailable" in page.text


def test_write_api_requires_admin_token_when_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "protected-write.db")
    db.initialize()
    monkeypatch.setattr(main_app, "db", db)
    monkeypatch.setattr(
        main_app, "settings", replace(main_app.settings, admin_token="test-secret")
    )
    with TestClient(main_app.app) as client:
        unauthorized = client.post("/api/events/999/evidence-bundle/rebuild")
        authorized = client.post(
            "/api/events/999/evidence-bundle/rebuild",
            headers={"x-admin-token": "test-secret"},
        )
    assert unauthorized.status_code == 401
    assert authorized.status_code == 404


def test_research_tool_audit_rejects_and_redacts_credentials(tmp_path: Path) -> None:
    db = Database(tmp_path / "credential-audit.db")
    db.initialize()
    _event_id, _evidence_ids, candidate_id = make_research_chain(db, ready=False)
    run = start_research_run(
        db,
        db.one("SELECT * FROM research_candidates WHERE id=?", (candidate_id,)),
        ResearchRunInput(executor_type="agent", executor_name="credential-test"),
    )
    with pytest.raises(ValueError, match="sensitive credential"):
        record_research_tool_call(
            db,
            run,
            ResearchToolResultInput(
                tool_name="fetch_public_page",
                request={"url": "https://example.com", "api_key": "do-not-store"},
                status="failed",
            ),
        )
    saved = record_research_tool_call(
        db,
        run,
        ResearchToolResultInput(
            tool_name="get_context",
            request={"event_id": 1},
            status="failed",
            error="provider failed: token=do-not-store Bearer another-secret",
        ),
    )
    assert "do-not-store" not in saved["error"]
    assert "another-secret" not in saved["error"]
    assert "[REDACTED]" in saved["error"]


def test_human_assessment_requires_bundle_evidence_and_approved_review_creates_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "assessment.db")
    db.initialize()
    event_id, evidence_ids, candidate_id = make_research_chain(db)
    run = start_research_run(
        db,
        db.one("SELECT * FROM research_candidates WHERE id=?", (candidate_id,)),
        ResearchRunInput(executor_type="human", executor_name="assessment-test"),
    )
    completed_run = complete_research_run(
        db, run, ResearchRunCompleteInput(status="completed")
    )
    other_event = db.execute(
        """INSERT INTO trend_events
        (canonical_title,normalized_title,first_seen_at,last_seen_at,created_at,updated_at)
        VALUES('other','other','2026-07-17','2026-07-17','2026-07-17','2026-07-17')"""
    )
    other_evidence = db.execute(
        """INSERT INTO evidence
        (event_id,kind,url,title,excerpt,fetched_at,evidence_type,fetch_status,source_name,quality_score)
        VALUES(?,'article','https://other.example/x','Other','Other complete evidence text long enough.',
        '2026-07-17','full_article','ready','other',0.9)""",
        (other_event,),
    )
    monkeypatch.setattr(main_app, "db", db)
    payload = {
        "assessment_status": "worth_following",
        "change_type": "居住空间约束持续变化",
        "consumer_relevance": "多来源显示小空间住户持续受到储物约束影响。",
        "durability": "跨季节持续",
        "lead_time_fit": "适合常规实体商品开发周期",
        "target_users": ["小空间住户"],
        "new_scenarios": ["动态调整储物空间"],
        "unmet_needs": ["现有固定结构无法灵活调整"],
        "related_product_categories": ["家居收纳"],
        "fact_claims": [
            {"claim": "约束在多个时间点重复出现", "evidence_ids": evidence_ids}
        ],
        "inferences": [
            {"claim": "值得继续验证实体收纳方向", "evidence_ids": evidence_ids}
        ],
        "evidence_ids": evidence_ids,
        "missing_evidence": ["平台需求数据"],
        "research_run_id": completed_run["id"],
    }
    with TestClient(main_app.app) as client:
        invalid = client.post(
            f"/api/research-candidates/{candidate_id}/assessments",
            json={
                **payload,
                "evidence_ids": [other_evidence],
                "fact_claims": [
                    {"claim": "cross event", "evidence_ids": [other_evidence]}
                ],
                "inferences": [],
            },
        )
        created = client.post(
            f"/api/research-candidates/{candidate_id}/assessments", json=payload
        )
        reviewed = client.post(
            f"/api/opportunity-assessments/{created.json()['id']}/review",
            json={"review_status": "approved", "note": "引用与变化判断已核对"},
        )
        reviewed_again = client.post(
            f"/api/opportunity-assessments/{created.json()['id']}/review",
            json={"review_status": "approved", "note": "重复请求"},
        )
        changed_review = client.post(
            f"/api/opportunity-assessments/{created.json()['id']}/review",
            json={"review_status": "rejected", "note": "尝试改写"},
        )
    assert invalid.status_code == 400
    assert created.status_code == 200
    assert reviewed.status_code == 200
    assert changed_review.status_code == 409
    signal = reviewed.json()["opportunity_signal"]
    assert signal["event_id"] == event_id
    assert signal["opportunity_assessment_id"] == created.json()["id"]
    assert signal["review_status"] == "follow_up"
    assert signal["product_opportunity_score"] == 0
    assert reviewed_again.json()["opportunity_signal"]["id"] == signal["id"]
    assert db.one("SELECT COUNT(*) n FROM opportunity_signals")["n"] == 1
    assert db.one("SELECT COUNT(*) n FROM product_hypotheses")["n"] == 0
    feedback = db.one(
        "SELECT * FROM opportunity_signal_feedback WHERE signal_id=?", (signal["id"],)
    )
    snapshot = json.loads(feedback["snapshot_json"])
    assert snapshot["event"]["id"] == event_id
    assert snapshot["evidence_bundle"]["id"]
    assert snapshot["research_candidate"]["id"] == candidate_id
    assert snapshot["opportunity_assessment"]["id"] == created.json()["id"]
    assert {item["id"] for item in snapshot["evidence"]} >= set(evidence_ids)


def test_event_workbench_completes_judgment_run_assessment_and_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "opportunity-judgment-workbench.db")
    db.initialize()
    event_id, evidence_ids, candidate_id = make_research_chain(db)
    monkeypatch.setattr(main_app, "db", db)
    payload = {
        "assessment": {
            "assessment_status": "worth_following",
            "change_type": "居住空间约束持续变化",
            "consumer_relevance": "多来源显示小空间住户反复调整收纳方式。",
            "durability": "变化跨季节存在，并非一次性热点。",
            "lead_time_fit": "持续时间覆盖常规实体商品开发和运输周期。",
            "target_users": ["小空间住户"],
            "new_scenarios": ["房间用途频繁切换"],
            "unmet_needs": ["固定结构无法随空间调整"],
            "related_product_categories": ["家居收纳"],
            "fact_claims": [
                {
                    "claim": "两个独立来源记录了重复出现的收纳约束。",
                    "evidence_ids": evidence_ids,
                }
            ],
            "inferences": [
                {
                    "claim": "该变化值得进入实体商品方向验证。",
                    "evidence_ids": evidence_ids,
                }
            ],
            "evidence_ids": evidence_ids,
            "missing_evidence": ["平台需求和竞争数据"],
        },
        "note": "页面内人工确认",
    }

    with TestClient(main_app.app) as client:
        before = client.get(f"/events/{event_id}")
        result = client.post(
            f"/api/research-candidates/{candidate_id}/opportunity-judgment",
            json=payload,
        )
        after = client.get(f"/events/{event_id}")

    assert before.status_code == 200
    assert 'id="opportunity-judgment-form"' not in before.text
    assert 'name="judgment-evidence"' not in before.text
    assert "进入判断任务" in before.text
    assert "手填证据 ID" not in before.text
    assert result.status_code == 200
    body = result.json()
    assert body["research_run"]["status"] == "completed"
    assert body["assessment"]["review_status"] == "approved"
    assert body["opportunity_signal"]["event_id"] == event_id
    assert db.one("SELECT COUNT(*) n FROM research_runs")["n"] == 1
    assert db.one("SELECT COUNT(*) n FROM opportunity_assessments")["n"] == 1
    assert db.one("SELECT COUNT(*) n FROM opportunity_signals")["n"] == 1
    assert db.one(
        "SELECT status FROM research_candidates WHERE id=?", (candidate_id,)
    )["status"] == "completed"
    assert 'id="opportunity-judgment-form"' not in after.text
    assert 'class="product-direction-form"' not in after.text
    assert "判断已完成" in after.text
    assert "引用证据 ID" not in after.text


def test_event_workbench_can_finish_with_more_evidence_needed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "opportunity-judgment-more-evidence.db")
    db.initialize()
    event_id, evidence_ids, candidate_id = make_research_chain(db, ready=False)
    monkeypatch.setattr(main_app, "db", db)

    with TestClient(main_app.app) as client:
        result = client.post(
            f"/api/research-candidates/{candidate_id}/opportunity-judgment",
            json={
                "assessment": {
                    "assessment_status": "insufficient_evidence",
                    "evidence_ids": evidence_ids,
                    "missing_evidence": ["第二个独立来源"],
                    "abstention_reason": "当前只有一个可分析来源，无法判断持续性。",
                },
                "note": "先补证据",
            },
        )

    assert result.status_code == 200
    assert result.json()["assessment"]["review_status"] == "needs_more_evidence"
    assert result.json()["opportunity_signal"] is None
    assert db.one(
        "SELECT status FROM research_candidates WHERE id=?", (candidate_id,)
    )["status"] == "insufficient_evidence"
    assert db.one("SELECT COUNT(*) n FROM opportunity_signals")["n"] == 0


def test_insufficient_assessment_cannot_be_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "insufficient-assessment.db")
    db.initialize()
    _event_id, evidence_ids, candidate_id = make_research_chain(db, ready=False)
    run = start_research_run(
        db,
        db.one("SELECT * FROM research_candidates WHERE id=?", (candidate_id,)),
        ResearchRunInput(executor_type="human", executor_name="insufficient-test"),
    )
    completed_run = complete_research_run(
        db, run, ResearchRunCompleteInput(status="completed")
    )
    monkeypatch.setattr(main_app, "db", db)
    with TestClient(main_app.app) as client:
        created = client.post(
            f"/api/research-candidates/{candidate_id}/assessments",
            json={
                "assessment_status": "insufficient_evidence",
                "evidence_ids": evidence_ids,
                    "missing_evidence": ["完整正文"],
                    "abstention_reason": "只有标题证据",
                    "research_run_id": completed_run["id"],
                },
        )
        reviewed = client.post(
            f"/api/opportunity-assessments/{created.json()['id']}/review",
            json={"review_status": "approved"},
        )
    assert created.status_code == 200
    assert reviewed.status_code == 409
    assert db.one("SELECT COUNT(*) n FROM opportunity_signals")["n"] == 0


@pytest.mark.asyncio
async def test_controlled_research_tools_resume_enforce_budget_and_deduplicate_evidence(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "research-tools.db")
    db.initialize()
    event_id, _evidence_ids, candidate_id = make_research_chain(db, ready=False)
    candidate = db.one("SELECT * FROM research_candidates WHERE id=?", (candidate_id,))
    run = start_research_run(
        db,
        candidate,
        ResearchRunInput(
            executor_type="agent",
            executor_name="test-agent",
            budget=ResearchBudget(max_fetch_pages=1, timeout_seconds=30),
        ),
    )

    async def fake_fetch(url: str, title: str) -> EvidenceResult:
        return EvidenceResult(
            url,
            title,
            "A complete public report documents recurring consumer constraints over time.",
            "2026-07-17T00:00:00+00:00",
            200,
            hashlib.sha256(url.encode()).hexdigest(),
        )

    executor = ResearchToolExecutor(db, fetcher=fake_fetch)
    request = {
        "url": "https://new-source.example/report",
        "title": "Independent report",
        "source_name": "new-source",
    }
    first = await executor.execute(run, "fetch_public_page", request)
    replayed = await ResearchToolExecutor(db, fetcher=fake_fetch).execute(
        run, "fetch_public_page", request
    )
    context = await ResearchToolExecutor(db, fetcher=fake_fetch).execute(
        run, "get_context", {}
    )
    assert first["replayed"] is False
    assert replayed["replayed"] is True
    assert first["tool_call"]["id"] == replayed["tool_call"]["id"]
    assert len(
        db.all(
            "SELECT * FROM evidence WHERE event_id=? AND url='https://new-source.example/report'",
            (event_id,),
        )
    ) == 1
    assert context["result"]["candidate"]["id"] == candidate_id
    assert context["result"]["candidate"]["evidence_bundle_id"] == first["result"][
        "evidence_bundle"
    ]["id"]
    assert context["result"]["candidate"]["evidence_bundle_id"] != candidate[
        "evidence_bundle_id"
    ]
    assert db.one("SELECT COUNT(*) n FROM research_tool_calls")["n"] == 2
    columns = {row["name"] for row in db.all("PRAGMA table_info(research_tool_calls)")}
    assert "request_json" not in columns
    with pytest.raises(ValueError, match="budget exhausted"):
        await executor.execute(
            run,
            "fetch_public_page",
            {"url": "https://third.example/report", "title": "Third"},
        )


@pytest.mark.asyncio
async def test_collect_related_news_falls_back_to_active_public_search(
    tmp_path: Path,
) -> None:
    class FakeProvider:
        name = "fake-search"

        async def search(self, query: str, **_kwargs) -> list[NewsSearchHit]:
            return [
                NewsSearchHit(
                    url="https://first-news.example/report",
                    title=f"{query} first report",
                    snippet="",
                    source_name="first-news",
                    published_at=None,
                    provider=self.name,
                    rank=1,
                    query_hash="1" * 64,
                ),
                NewsSearchHit(
                    url="https://second-press.example/report",
                    title=f"{query} second report",
                    snippet="",
                    source_name="second-press",
                    published_at=None,
                    provider=self.name,
                    rank=2,
                    query_hash="1" * 64,
                ),
            ]

    db = Database(tmp_path / "active-news-search.db")
    db.initialize()
    event_id, _evidence_ids, candidate_id = make_research_chain(db, ready=False)
    candidate = db.one("SELECT * FROM research_candidates WHERE id=?", (candidate_id,))
    run = start_research_run(
        db,
        candidate,
        ResearchRunInput(
            executor_type="agent",
            executor_name="search-agent",
            budget=ResearchBudget(
                max_search_queries=1,
                max_fetch_pages=2,
                timeout_seconds=30,
            ),
        ),
    )

    async def fake_fetch(url: str, title: str) -> EvidenceResult:
        excerpt = FULL_ARTICLE_ONE if "first-news" in url else FULL_ARTICLE_TWO
        return EvidenceResult(
            url,
            title,
            excerpt,
            "2026-07-17T00:00:00+00:00",
            200,
            hashlib.sha256(url.encode()).hexdigest(),
        )

    result = await ResearchToolExecutor(
        db,
        fetcher=fake_fetch,
        news_search_provider=FakeProvider(),
    ).execute(run, "collect_related_news", {})

    saved = db.all(
        "SELECT * FROM evidence WHERE event_id=? AND fetch_method='public-news-search' ORDER BY id",
        (event_id,),
    )
    assert result["replayed"] is False
    assert len(saved) == 2
    assert all(json.loads(row["raw_metadata_json"])["search_query_hash"] for row in saved)
    assert result["result"]["evidence_bundle"]["independent_source_count"] == 2
    assert result["result"]["evidence_bundle"]["readiness_status"] == "ready_for_assessment"


def test_opportunity_assessment_v2_requires_consistent_three_level_judgment() -> None:
    evidence_ids = [1, 2]
    value = OpportunityAssessmentDraft(
        assessment_status="worth_following",
        change_type="出行规则长期收紧",
        consumer_relevance="普通旅客的携带方式受到影响。",
        durability="长期政策变化。",
        lead_time_fit="有时间继续研究。",
        target_users=["经常乘坐廉价航空的旅客"],
        new_scenarios=["登机前核对随身行李尺寸"],
        existing_solutions=["标称登机尺寸的旅行包"],
        solution_gaps=["装满后实际尺寸难以判断"],
        unmet_needs=["出发前可靠确认是否超限"],
        consumer_change_judgment={
            "status": "related", "rationale": "影响消费者出行", "evidence_ids": evidence_ids
        },
        problem_judgment={
            "status": "clear", "rationale": "出现额外费用和判断成本", "evidence_ids": evidence_ids
        },
        research_recommendation={
            "status": "continue_research", "rationale": "值得继续核实消费者反馈", "evidence_ids": evidence_ids
        },
        fact_claims=[{"claim": "规则已开始执行。", "evidence_ids": evidence_ids}],
        evidence_ids=evidence_ids,
    )
    value.require_v2()
    with pytest.raises(ValueError, match="assessment status must be insufficient_evidence"):
        OpportunityAssessmentDraft.model_validate(
            {
                **value.model_dump(),
                "problem_judgment": {
                    "status": "needs_evidence",
                    "rationale": "仍缺消费者反馈",
                    "evidence_ids": evidence_ids,
                },
            }
        )
    with pytest.raises(ValueError, match="must not output product categories"):
        forbidden = value.model_copy(update={"related_product_categories": ["旅行包"]})
        forbidden.require_v2()


@pytest.mark.asyncio
async def test_cloud_assessment_preflight_gates_model_and_failure_is_explicit() -> None:
    class FakeResponses:
        def __init__(self, parsed=None, error: Exception | None = None):
            self.parsed = parsed
            self.error = error
            self.calls = 0

        async def parse(self, **_kwargs):
            self.calls += 1
            if self.error:
                raise self.error
            return SimpleNamespace(output_parsed=self.parsed, output=[])

    class FakeClient:
        def __init__(self, responses):
            self.responses = responses

    insufficient_responses = FakeResponses()
    insufficient_provider = CloudOpportunityAssessmentProvider(
        "key", "test-model", client=FakeClient(insufficient_responses)
    )
    insufficient = await insufficient_provider.assess(
        {"id": 1, "canonical_title": "Rain", "market": "CN"},
        {
            "readiness_status": "insufficient",
            "evidence_ids": [1],
            "missing_evidence": ["完整正文"],
        },
        {"id": 1},
        [{"id": 1}],
    )
    assert insufficient.draft.assessment_status == "insufficient_evidence"
    assert insufficient_responses.calls == 0

    sensitive_responses = FakeResponses()
    sensitive_provider = CloudOpportunityAssessmentProvider(
        "key", "test-model", client=FakeClient(sensitive_responses)
    )
    sensitive = await sensitive_provider.assess(
        {"id": 2, "canonical_title": "枪击事件造成伤亡", "market": "CN"},
        {"readiness_status": "ready_for_assessment", "evidence_ids": [2]},
        {"id": 2},
        [{"id": 2}],
    )
    assert sensitive.draft.assessment_status == "abstained"
    assert sensitive_responses.calls == 0

    failed_responses = FakeResponses(error=RuntimeError("provider unavailable"))
    failed_provider = CloudOpportunityAssessmentProvider(
        "key", "test-model", client=FakeClient(failed_responses)
    )
    failed = await failed_provider.assess(
        {"id": 3, "canonical_title": "Recurring storage", "market": "US"},
        {"readiness_status": "ready_for_assessment", "evidence_ids": [3]},
        {"id": 3},
        [{"id": 3, "title": "Report", "excerpt": "Complete report"}],
    )
    assert failed.draft.assessment_status == "abstained"
    assert "provider unavailable" in failed.draft.abstention_reason
    assert failed.engine.endswith("-failed")
    assert failed.generation_status == "failed"


@pytest.mark.asyncio
async def test_cloud_assessment_unknown_citation_is_rejected_after_structured_output() -> None:
    parsed = OpportunityAssessmentDraft(
        assessment_status="worth_following",
        change_type="空间约束变化",
        consumer_relevance="住户持续受到影响",
        durability="长期",
        lead_time_fit="匹配",
        target_users=["小空间住户"],
        new_scenarios=["动态收纳"],
        existing_solutions=["固定层架"],
        solution_gaps=["无法灵活调整"],
        unmet_needs=["灵活结构"],
        consumer_change_judgment={
            "status": "related", "rationale": "事实支持", "evidence_ids": [999]
        },
        problem_judgment={
            "status": "clear", "rationale": "问题明确", "evidence_ids": [999]
        },
        research_recommendation={
            "status": "continue_research", "rationale": "值得继续", "evidence_ids": [999]
        },
        fact_claims=[{"claim": "事实", "evidence_ids": [999]}],
        evidence_ids=[999],
    )

    class FakeResponses:
        async def parse(self, **_kwargs):
            return SimpleNamespace(output_parsed=parsed, output=[])

    provider = CloudOpportunityAssessmentProvider(
        "key", "test-model", client=SimpleNamespace(responses=FakeResponses())
    )
    result = await provider.assess(
        {"id": 1, "canonical_title": "Recurring storage", "market": "US"},
        {"readiness_status": "ready_for_assessment", "evidence_ids": [1]},
        {"id": 1, "event_id": 1},
        [{"id": 1, "event_id": 1, "title": "Report", "excerpt": "Complete report"}],
    )
    with pytest.raises(ValueError, match="unknown or cross-event"):
        validate_assessment_evidence(
            {"event_id": 1},
            {"evidence_ids": [1]},
            [{"id": 1, "event_id": 1}],
            result.draft,
        )
    with pytest.raises(ValueError):
        OpportunityAssessmentDraft.model_validate(
            {
                **parsed.model_dump(),
                "product_hypothesis": {"name": "forbidden"},
            }
        )


def test_research_workbench_generates_pending_ai_draft_then_approves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "research-workbench.db")
    db.initialize()
    event_id, evidence_ids, candidate_id = make_research_chain(db)

    class FakeCloudProvider:
        def __init__(self, _api_key, model, **_kwargs):
            self.model = model

        async def assess(self, _event, _bundle, _candidate, _evidence):
            return SimpleNamespace(
                draft=OpportunityAssessmentDraft(
                    assessment_status="worth_following",
                    change_type="居住空间约束持续变化",
                    consumer_relevance="住户持续调整收纳方式并产生重复购买。",
                    durability="变化跨季节持续存在。",
                    lead_time_fit="持续时间覆盖常规实体商品交付周期。",
                    target_users=["小空间住户"],
                    new_scenarios=["房间用途频繁切换"],
                    existing_solutions=["固定收纳架"],
                    solution_gaps=["无法随空间变化调整"],
                    unmet_needs=["固定结构无法灵活调整"],
                    consumer_change_judgment={
                        "status": "related",
                        "rationale": "两个来源记录了持续变化。",
                        "evidence_ids": evidence_ids,
                    },
                    problem_judgment={
                        "status": "clear",
                        "rationale": "固定方案带来明确不便。",
                        "evidence_ids": evidence_ids,
                    },
                    research_recommendation={
                        "status": "continue_research",
                        "rationale": "问题持续且仍需进一步验证。",
                        "evidence_ids": evidence_ids,
                    },
                    fact_claims=[
                        {"claim": "两个独立来源记录了持续的空间约束。", "evidence_ids": evidence_ids}
                    ],
                    inferences=[
                        {"claim": "该变化值得继续研究。", "evidence_ids": evidence_ids}
                    ],
                    evidence_ids=evidence_ids,
                    missing_evidence=["平台需求数据"],
                ),
                engine="cloud-opportunity-assessment",
                model=self.model,
                version="cloud-assessment-test",
            )

    monkeypatch.setattr(main_app, "db", db)
    monkeypatch.setattr(main_app, "CloudOpportunityAssessmentProvider", FakeCloudProvider)
    monkeypatch.setattr(
        main_app,
        "settings",
        replace(main_app.settings, openai_api_key="test-key", openai_model="test-model"),
    )

    with TestClient(main_app.app) as client:
        pending_before = client.get("/workbench")
        detail_before = client.get(f"/workbench/items/{candidate_id}")
        generated = client.post(
            f"/api/workbench/research-candidates/{candidate_id}/ai-draft"
        )
        detail_after = client.get(f"/workbench/items/{candidate_id}")
        generated_again = client.post(
            f"/api/workbench/research-candidates/{candidate_id}/ai-draft"
        )
        signals_after_draft = db.one("SELECT COUNT(*) n FROM opportunity_signals")["n"]
        reviewed = client.post(
            f"/api/opportunity-assessments/{generated.json()['id']}/review",
            json={
                "review_status": "approved",
                "fact_check": "accurate",
                "problem_check": "real",
                "durability_check": "sufficient",
                "note": "已核对事实与引用",
            },
        )
        pending_after = client.get("/workbench")
        processed_after = client.get("/workbench/processed")

    assert pending_before.status_code == 200
    assert "Recurring small-space constraints" in pending_before.text
    assert "生成 AI 判断卡" in pending_before.text
    assert "生成三级判断卡" in detail_before.text
    assert generated.status_code == 200
    assert generated.json()["review_status"] == "pending"
    assert generated_again.json()["id"] == generated.json()["id"]
    assert signals_after_draft == 0
    assert db.one("SELECT COUNT(*) n FROM opportunity_assessments")["n"] == 1
    assert db.one("SELECT COUNT(*) n FROM opportunity_signals")["n"] == 1
    assert "居住空间约束持续变化" in detail_after.text
    assert "小空间住户" in detail_after.text
    assert "房间用途频繁切换" in detail_after.text
    assert "固定结构无法灵活调整" in detail_after.text
    assert "固定收纳架" in detail_after.text
    assert "无法随空间变化调整" in detail_after.text
    assert "平台需求数据" in detail_after.text
    assert "证据 #" in detail_after.text
    assert "继续研究" in detail_after.text
    assert "补充证据" in detail_after.text
    assert "放弃" in detail_after.text
    assert "技术与审计信息" in detail_after.text
    assert reviewed.status_code == 200
    assert reviewed.json()["opportunity_signal"]["event_id"] == event_id
    assert "Recurring small-space constraints" not in pending_after.text
    assert "Recurring small-space constraints" in processed_after.text
    assert "已决定继续研究" in processed_after.text


@pytest.mark.parametrize(
    ("review_status", "expected_candidate_status", "processed"),
    [
        ("needs_more_evidence", "insufficient_evidence", False),
        ("rejected", "completed", True),
    ],
)
def test_research_workbench_nonapproval_never_creates_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    review_status: str,
    expected_candidate_status: str,
    processed: bool,
) -> None:
    db = Database(tmp_path / f"workbench-{review_status}.db")
    db.initialize()
    _event_id, evidence_ids, candidate_id = make_research_chain(db)
    run = start_research_run(
        db,
        db.one("SELECT * FROM research_candidates WHERE id=?", (candidate_id,)),
        ResearchRunInput(executor_type="human", executor_name="workbench-test"),
    )
    completed_run = complete_research_run(
        db, run, ResearchRunCompleteInput(status="completed")
    )
    monkeypatch.setattr(main_app, "db", db)
    with TestClient(main_app.app) as client:
        assessment = client.post(
            f"/api/research-candidates/{candidate_id}/assessments",
            json={
                "assessment_status": "worth_following",
                "change_type": "空间约束变化",
                "consumer_relevance": "住户持续受到影响。",
                "durability": "跨季节持续。",
                "lead_time_fit": "匹配交付周期。",
                "target_users": ["小空间住户"],
                "new_scenarios": ["动态收纳"],
                "unmet_needs": ["灵活结构"],
                "related_product_categories": ["家居收纳"],
                "fact_claims": [{"claim": "事实", "evidence_ids": evidence_ids}],
                "inferences": [{"claim": "推断", "evidence_ids": evidence_ids}],
                "evidence_ids": evidence_ids,
                "research_run_id": completed_run["id"],
            },
        )
        review = client.post(
            f"/api/opportunity-assessments/{assessment.json()['id']}/review",
            json={"review_status": review_status, "note": "页面审核"},
        )
        pending_page = client.get("/workbench")
        processed_page = client.get("/workbench/processed")

    assert review.status_code == 200
    assert review.json()["opportunity_signal"] is None
    assert db.one("SELECT COUNT(*) n FROM opportunity_signals")["n"] == 0
    assert db.one(
        "SELECT status FROM research_candidates WHERE id=?", (candidate_id,)
    )["status"] == expected_candidate_status
    assert ("Recurring small-space constraints" in processed_page.text) is processed
    assert ("Recurring small-space constraints" in pending_page.text) is (not processed)


def test_workbench_supplement_creates_versioned_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "workbench-supplement.db")
    db.initialize()
    event_id, evidence_ids, candidate_id = make_research_chain(db)
    original_bundle_id = db.one(
        "SELECT evidence_bundle_id FROM research_candidates WHERE id=?", (candidate_id,)
    )["evidence_bundle_id"]
    run = start_research_run(
        db,
        db.one("SELECT * FROM research_candidates WHERE id=?", (candidate_id,)),
        ResearchRunInput(executor_type="human", executor_name="supplement-test"),
    )
    completed = complete_research_run(
        db, run, ResearchRunCompleteInput(status="completed")
    )

    async def public_url(_url: str) -> bool:
        return True

    monkeypatch.setattr(main_app, "db", db)
    monkeypatch.setattr(main_app, "is_public_url", public_url)
    assessment_payload = {
        "assessment_status": "worth_following",
        "change_type": "空间约束持续变化",
        "consumer_relevance": "普通住户持续受到影响。",
        "durability": "跨季节持续。",
        "lead_time_fit": "适合继续研究。",
        "target_users": ["小空间住户"],
        "new_scenarios": ["动态调整收纳空间"],
        "existing_solutions": ["固定收纳架"],
        "solution_gaps": ["不能灵活调整"],
        "unmet_needs": ["更灵活的处理方式"],
        "consumer_change_judgment": {
            "status": "related", "rationale": "影响普通消费者", "evidence_ids": evidence_ids,
        },
        "problem_judgment": {
            "status": "clear", "rationale": "存在明确限制", "evidence_ids": evidence_ids,
        },
        "research_recommendation": {
            "status": "continue_research", "rationale": "值得补充消费者反馈", "evidence_ids": evidence_ids,
        },
        "fact_claims": [{"claim": "多个来源记录了变化。", "evidence_ids": evidence_ids}],
        "evidence_ids": evidence_ids,
        "missing_evidence": ["缺少消费者真实使用反馈"],
        "research_run_id": completed["id"],
    }
    with TestClient(main_app.app) as client:
        assessment = client.post(
            f"/api/research-candidates/{candidate_id}/assessments",
            json=assessment_payload,
        )
        reviewed = client.post(
            f"/api/opportunity-assessments/{assessment.json()['id']}/review",
            json={
                "review_status": "needs_more_evidence",
                "fact_check": "accurate",
                "problem_check": "uncertain",
                "durability_check": "sufficient",
                "selected_missing_evidence": ["缺少消费者真实使用反馈"],
                "note": "先补一条消费者讨论",
            },
        )
        supplemented = client.post(
            f"/api/workbench/research-candidates/{candidate_id}/evidence",
            json={
                "source_name": "Consumer Forum",
                "url": "https://consumer.example/discussion",
                "title": "Residents discuss changing storage constraints",
                "excerpt": "Residents repeatedly describe the same constraint and the time cost of rearranging fixed shelves. " * 4,
                "is_consumer_voice": True,
                "addressed_missing_evidence": ["缺少消费者真实使用反馈"],
            },
        )
        duplicate = client.post(
            f"/api/workbench/research-candidates/{candidate_id}/evidence",
            json={
                "source_name": "Consumer Forum",
                "url": "https://consumer.example/discussion",
                "title": "Duplicate",
                "excerpt": "Duplicate evidence should not rewrite the old task. " * 4,
                "is_consumer_voice": True,
                "addressed_missing_evidence": ["缺少消费者真实使用反馈"],
            },
        )

    assert assessment.status_code == 200
    assert reviewed.status_code == 200
    assert supplemented.status_code == 200
    successor = supplemented.json()["candidate"]
    assert successor["id"] != candidate_id
    assert successor["evidence_bundle_id"] != original_bundle_id
    assert supplemented.json()["redirect_url"] == f"/workbench/items/{successor['id']}"
    assert db.one("SELECT status FROM research_candidates WHERE id=?", (candidate_id,))[
        "status"
    ] == "superseded"
    stored_review = json.loads(
        db.one(
            "SELECT review_details_json FROM opportunity_assessments WHERE id=?",
            (assessment.json()["id"],),
        )["review_details_json"]
    )
    assert stored_review["superseded_by_candidate_id"] == successor["id"]
    assert db.one("SELECT COUNT(*) n FROM evidence WHERE event_id=?", (event_id,))["n"] == 3
    assert duplicate.status_code == 409


def test_workbench_failed_generation_is_retryable_and_never_reviewable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "workbench-retry.db")
    db.initialize()
    _event_id, evidence_ids, candidate_id = make_research_chain(db)

    class RetryProvider:
        calls = 0

        def __init__(self, _api_key, model, **_kwargs):
            self.model = model

        async def assess(self, _event, _bundle, _candidate, _evidence):
            type(self).calls += 1
            if type(self).calls == 1:
                return SimpleNamespace(
                    draft=OpportunityAssessmentDraft(
                        assessment_status="abstained",
                        evidence_ids=evidence_ids,
                        abstention_reason="provider unavailable",
                    ),
                    engine="cloud-opportunity-assessment-failed",
                    model=self.model,
                    version="cloud-assessment-v2",
                    generation_status="failed",
                )
            return SimpleNamespace(
                draft=OpportunityAssessmentDraft(
                    assessment_status="insufficient_evidence",
                    change_type="空间约束变化",
                    consumer_relevance="可能影响普通住户。",
                    durability="持续性仍需核实。",
                    lead_time_fit="暂不判断。",
                    target_users=["小空间住户"],
                    new_scenarios=["动态调整空间"],
                    consumer_change_judgment={
                        "status": "related", "rationale": "有事实支持", "evidence_ids": evidence_ids,
                    },
                    problem_judgment={
                        "status": "needs_evidence", "rationale": "缺少用户反馈", "evidence_ids": evidence_ids,
                    },
                    research_recommendation={
                        "status": "defer", "rationale": "先补证", "evidence_ids": evidence_ids,
                    },
                    fact_claims=[{"claim": "变化已发生。", "evidence_ids": evidence_ids}],
                    evidence_ids=evidence_ids,
                    missing_evidence=["消费者反馈"],
                    abstention_reason="证据不足，暂缓判断。",
                ),
                engine="cloud-opportunity-assessment",
                model=self.model,
                version="cloud-assessment-v2",
                generation_status="completed",
            )

    monkeypatch.setattr(main_app, "db", db)
    monkeypatch.setattr(main_app, "CloudOpportunityAssessmentProvider", RetryProvider)
    monkeypatch.setattr(
        main_app,
        "settings",
        replace(main_app.settings, openai_api_key="test-key", openai_model="test-model"),
    )
    with TestClient(main_app.app) as client:
        failed = client.post(
            f"/api/workbench/research-candidates/{candidate_id}/ai-draft"
        )
        rejected_review = client.post(
            f"/api/opportunity-assessments/{failed.json()['id']}/review",
            json={"review_status": "rejected"},
        )
        retried = client.post(
            f"/api/workbench/research-candidates/{candidate_id}/ai-draft"
        )

    assert failed.status_code == 200
    assert failed.json()["generation_status"] == "failed"
    assert failed.json()["review_status"] == "superseded"
    assert rejected_review.status_code == 409
    assert retried.status_code == 200
    assert retried.json()["generation_status"] == "completed"
    assert retried.json()["review_status"] == "pending"
    assert db.one("SELECT COUNT(*) n FROM opportunity_assessments")["n"] == 2


def test_research_workbench_blocks_ai_when_bundle_is_not_ready_and_hides_old_nav(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "workbench-insufficient.db")
    db.initialize()
    _event_id, _evidence_ids, candidate_id = make_research_chain(db, ready=False)
    monkeypatch.setattr(main_app, "db", db)
    monkeypatch.setattr(
        main_app, "settings", replace(main_app.settings, openai_api_key="test-key")
    )
    with TestClient(main_app.app) as client:
        page = client.get("/workbench")
        detail = client.get(f"/workbench/items/{candidate_id}")
        generated = client.post(
            f"/api/workbench/research-candidates/{candidate_id}/ai-draft"
        )
        legacy = client.get("/research")

    assert page.status_code == 200
    assert "判断任务" in page.text and "判断记录" in page.text
    assert ">商品方向<" not in page.text
    assert ">市场验证<" not in page.text
    assert ">已验证选品<" not in page.text
    assert "关键证据尚未就绪" in page.text
    assert "先补齐关键证据" in detail.text
    assert generated.status_code == 409
    assert db.one("SELECT COUNT(*) n FROM research_runs")["n"] == 0
    assert db.one("SELECT COUNT(*) n FROM opportunity_assessments")["n"] == 0
    assert legacy.status_code == 200
