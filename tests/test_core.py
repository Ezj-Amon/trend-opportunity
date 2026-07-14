from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.main as main_app
from app.analysis import Analyzer
from app.clustering import normalize_title, should_merge, title_similarity
from app.config import Settings
from app.db import Database
from app.pipeline import Pipeline
from app.scoring import calculate_evidence_confidence, calculate_opportunity_score


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
    }.issubset(tables)
    assert {"market", "language", "signal_type"}.issubset(
        {row["name"] for row in db.all("PRAGMA table_info(trend_events)")}
    )
    assert "marketplace" in {
        row["name"] for row in db.all("PRAGMA table_info(product_opportunities)")
    }
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
