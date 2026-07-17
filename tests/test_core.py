from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.main as main_app
import app.pipeline as pipeline_module
from app.analysis import Analyzer
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
from app.evidence import EvidenceResult
from app.market_validation import MarketScores
from app.pipeline import Pipeline
from app.reports import build_daily_digest
from app.risk import assess_product_risk
from app.scoring import calculate_evidence_confidence, calculate_opportunity_score
from app.scoring import calculate_final_score, calculate_market_score


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "test.db",
        newsnow_base_url="https://newsnow.busiyi.world",
        source_ids=("weibo",),
        analysis_top_n=5,
        overseas_analysis_top_n=5,
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
    )


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
    assert (final, penalty) == (50.0, 30.0)
    blocked, _ = calculate_final_score(
        trend_score=100,
        hypothesis_score=100,
        market_score=100,
        validation_status="completed",
        risk_level="blocking",
    )
    assert blocked == 0


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
        differentiation_score,opportunity_score,evidence_confidence,review_status,created_at,updated_at)
        VALUES(?,?,'n','s','s','j','[]','x','m','p','[]','[]',1,1,1,1,1,1,0,0,'approved',?,?)""",
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
    assert all(item["risk_level"] != "blocking" for item in [*digest["cn_top3"], *digest["overseas_top3"]])
    assert len({item["name"] for item in digest["cn_top3"]}) == 3
    assert len({item["name"] for item in digest["overseas_top3"]}) == 3


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
        names = [item["name"] for item in queue.json()]
        assert len([name for name in names if normalized_identity(name) == "新技术选购包"]) == 1


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
        assert "最终排序分" in detail.text
        assert "SellerSprite export" in detail.text
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "中国信号 Top 3" in dashboard.text
        assert "海外信号 Top 3" in dashboard.text
    saved = db.one("SELECT * FROM product_opportunities WHERE id=?", (opportunity_id,))
    assert saved["validation_status"] == "completed"
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
        assert queue.json()[0]["signal_market"] == "CN"
        assert queue.json()[0]["target_marketplace"] == "US"
        assert queue.json()[0]["query_readiness"] == "ready"
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
        assert "CN → US" in page.text
        template = client.get("/api/market-validations/template.csv?marketplace=US")
        assert template.status_code == 200
        assert "backpack" in template.text

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
async def test_pipeline_persists_v2_scores_risks_and_explicit_missing_validation(
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
        return EvidenceResult(url, title, "Shoppers compare durable storage products for small homes.", now, 200, "hash")

    monkeypatch.setattr(pipeline_module, "fetch_evidence", fake_fetch)
    pipeline = Pipeline(db, make_settings(tmp_path))
    await pipeline._research_and_analyze("r", event_id)
    opportunities = db.all("SELECT * FROM product_opportunities WHERE event_id=?", (event_id,))
    assert opportunities
    assert all(item["score_formula_version"] == "opportunity-v2" for item in opportunities)
    assert all(item["hypothesis_score"] >= item["opportunity_score"] for item in opportunities)
    assert all(item["market_score"] is None for item in opportunities)
    assert all(item["validation_status"] == "unavailable" for item in opportunities)
    assert all(item["product_keywords_json"] != "[]" for item in opportunities)
    validations = db.all("SELECT * FROM market_validations")
    assert len(validations) == len(opportunities)
    assert all(item["provider"] == "unconfigured" for item in validations)
    assert all("尚未录入" in item["error"] for item in validations)


@pytest.mark.asyncio
async def test_sensitive_event_is_not_commercialized(tmp_path: Path) -> None:
    analyzer = Analyzer(make_settings(tmp_path))
    result = await analyzer.analyze(
        {"canonical_title": "中国籍女医生在海外遇害", "trend_score": 80},
        [{"id": 1, "excerpt": "警方正在调查遇害案件"}],
    )
    assert result.output.opportunities == []
    assert "不生成商业化" in result.output.inference_notice


@pytest.mark.asyncio
async def test_sensitive_gate_runs_before_configured_llm(tmp_path: Path) -> None:
    configured = replace(make_settings(tmp_path), openai_api_key="not-a-real-key")
    result = await Analyzer(configured).analyze(
        {"canonical_title": "枪击事件造成伤亡", "trend_score": 90},
        [{"id": 1, "excerpt": "多人伤亡"}],
    )
    assert result.engine == "safety-gate"
    assert result.output.opportunities == []


@pytest.mark.asyncio
async def test_high_risk_device_operation_is_not_commercialized(tmp_path: Path) -> None:
    result = await Analyzer(make_settings(tmp_path)).analyze(
        {"canonical_title": "远程解锁BL锁，黑砖概不负责", "trend_score": 88},
        [{"id": 1, "excerpt": "提供远程测试"}],
    )
    assert result.engine == "safety-gate"
    assert result.output.opportunities == []


@pytest.mark.asyncio
async def test_child_injury_is_not_commercialized(tmp_path: Path) -> None:
    result = await Analyzer(make_settings(tmp_path)).analyze(
        {"canonical_title": "17天新生儿被宠物狗咬伤脑袋", "trend_score": 75},
        [{"id": 1, "excerpt": "新生儿接受治疗"}],
    )
    assert result.engine == "safety-gate"
    assert result.output.opportunities == []


@pytest.mark.asyncio
async def test_law_enforcement_event_is_not_commercialized(tmp_path: Path) -> None:
    result = await Analyzer(make_settings(tmp_path)).analyze(
        {"canonical_title": "严打编造传播涉汛等涉灾网络谣言", "trend_score": 70},
        [{"id": 1, "excerpt": "公安机关开展专项工作部署"}],
    )
    assert result.engine == "safety-gate"
    assert result.output.opportunities == []


@pytest.mark.asyncio
async def test_local_rules_are_labeled_as_inference(tmp_path: Path) -> None:
    analyzer = Analyzer(make_settings(tmp_path))
    result = await analyzer.analyze(
        {"canonical_title": "新台风生成影响周末出行", "trend_score": 65},
        [{"id": 1, "excerpt": "多地旅客关注航班和天气变化"}],
    )
    assert result.engine == "local-rules"
    assert result.output.opportunities
    assert "待验证推断" in result.output.inference_notice


@pytest.mark.asyncio
async def test_overseas_rules_bind_opportunity_to_amazon_marketplace(tmp_path: Path) -> None:
    result = await Analyzer(make_settings(tmp_path)).analyze(
        {
            "canonical_title": "Best storage organizer for small apartments",
            "trend_score": 78,
            "market": "US",
            "signal_type": "search",
        },
        [{"id": 1, "excerpt": "Shoppers compare durable home storage options"}],
    )
    assert result.output.opportunities
    opportunity = result.output.opportunities[0]
    assert opportunity.marketplace == "Amazon.com"
    assert opportunity.price_band.startswith("$")
    assert "Amazon.com" in opportunity.channels
    assert any("Amazon.com" in risk for risk in opportunity.risks)


@pytest.mark.asyncio
async def test_english_keyword_matching_uses_word_boundaries(tmp_path: Path) -> None:
    result = await Analyzer(make_settings(tmp_path)).analyze(
        {"canonical_title": "New studies show an unusual result", "trend_score": 70},
        [{"id": 1, "excerpt": "Researchers said more evidence is needed"}],
    )
    assert result.engine == "local-rules"
    assert result.output.opportunities == []


def test_overseas_analysis_quota_is_not_crowded_out(tmp_path: Path) -> None:
    db = Database(tmp_path / "quota.db")
    db.initialize()
    pipeline = Pipeline(db, replace(make_settings(tmp_path), analysis_top_n=1, overseas_analysis_top_n=1))
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


@pytest.mark.asyncio
async def test_local_rules_abstain_for_unknown_demand_category(tmp_path: Path) -> None:
    result = await Analyzer(make_settings(tmp_path)).analyze(
        {"canonical_title": "某队申请半决赛穿客场队服", "trend_score": 70},
        [{"id": 1, "excerpt": "球队公布比赛安排"}],
    )
    assert result.output.opportunities == []
    assert "主动弃权" in result.output.inference_notice


@pytest.mark.asyncio
async def test_llm_failure_remains_explicit_when_rules_abstain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    analyzer = Analyzer(replace(make_settings(tmp_path), openai_api_key="configured"))

    async def fail(*_args, **_kwargs):
        raise ValueError("invalid model output")

    monkeypatch.setattr(analyzer, "_analyze_with_llm", fail)
    result = await analyzer.analyze(
        {"canonical_title": "某队申请半决赛穿客场队服", "trend_score": 70},
        [{"id": 1, "excerpt": "球队公布比赛安排"}],
    )
    assert result.engine == "local-rules-fallback"
    assert result.degraded_reason
    assert "明确降级" in result.output.inference_notice
