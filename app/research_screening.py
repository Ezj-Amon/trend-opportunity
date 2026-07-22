from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

from .db import Database


SCREENING_VERSION = "research-screening-v2"
SCREENING_REVIEW_DECISIONS = {"collect_limited_evidence", "reject"}


EXCLUSION_RULES: dict[str, tuple[str, ...]] = {
    "disaster_or_casualty": (
        "遇害",
        "死亡",
        "去世",
        "逝世",
        "伤亡",
        "重伤",
        "伤员",
        "咬伤",
        "战争",
        "被困",
        "搜救",
        "救援现场",
        "山体垮塌",
        "山体崩塌",
        "山体滑坡",
        "泥石流",
        "建筑坍塌",
        "房屋坍塌",
        "桥梁坍塌",
        "地震救援",
        "洪灾",
        "灾情",
        "沉船",
        "游轮沉没",
        "击沉游轮",
        "killed",
        "dies",
        "dead",
        "death",
        "fatal",
        "victim",
        "injured",
        "war",
        "rescue operation",
        "landslide",
        "building collapse",
        "shipwreck",
        "cruise ship sank",
    ),
    "crime_or_harm": (
        "谋杀",
        "枪击",
        "性侵",
        "家暴",
        "家庭暴力",
        "自杀",
        "坠亡",
        "刑事拘留",
        "入室盗窃",
        "盗窃案",
        "抢劫",
        "绑架",
        "拐卖",
        "警方通报",
        "公安通报",
        "立案调查",
        "严打",
        "谣言被拘",
        "murder",
        "shooting",
        "assault",
        "kidnapping",
        "domestic violence",
        "破解",
        "绕过验证",
        "解锁bl",
        "远程测试",
    ),
    "sports_or_match": (
        "世界杯",
        "欧冠",
        "决赛",
        "半决赛",
        "夺冠",
        "比分",
        "赛程",
        "球员",
        "球队",
        "world cup",
        "championship final",
        "match result",
        "playoffs",
        "footballer",
    ),
    "person_or_gossip": (
        "明星恋情",
        "官宣恋情",
        "明星离婚",
        "明星结婚",
        "演员去世",
        "歌手去世",
        "网红",
        "celebrity",
        "dating rumor",
        "divorce rumor",
    ),
    "software_or_digital": (
        "github",
        "openai",
        "hugging face",
        "copilot",
        "posthog",
        "grok",
        "代码仓库",
        "开源项目",
        "编程教程",
        "软件更新",
        "大模型发布",
        "ai 模型",
        "ai model",
        "source code",
        "code repository",
        "developer sdk",
        "software release",
        "mobile app",
        "saas",
        "plugin",
    ),
    "medical_or_regulated": (
        "癌症治疗",
        "治愈",
        "特效药",
        "处方药",
        "减肥药",
        "medical treatment",
        "cancer cure",
        "prescription drug",
    ),
    "political_personnel": (
        "被双开",
        "开除党籍",
        "开除公职",
        "严重违纪违法",
        "审查调查",
        "总统大选",
        "选举结果",
        "election result",
    ),
}

ONE_OFF_EVENT_TERMS = (
    "暴雨",
    "台风登陆",
    "突发地震",
    "森林火灾",
    "山火",
    "洪水来袭",
    "今晚",
    "本周末",
    "今日开幕",
    "breaking news",
    "tonight only",
)

DURABILITY_TERMS = (
    "持续",
    "长期",
    "结构性",
    "趋势",
    "频发",
    "常态化",
    "每年",
    "逐年",
    "老龄化",
    "独居",
    "租房",
    "小户型",
    "禁售",
    "新规",
    "法规",
    "政策",
    "到2030",
    "到 2030",
    "recurring",
    "long-term",
    "long term",
    "structural",
    "trend",
    "increasingly",
    "every year",
    "regulation",
    "phase-out",
)

PHYSICAL_CONSUMPTION_TERMS = (
    "消费者",
    "家庭",
    "家居",
    "居住",
    "收纳",
    "整理",
    "厨房",
    "清洁",
    "户外",
    "露营",
    "通勤",
    "出行",
    "宠物",
    "母婴",
    "婴儿",
    "老人",
    "睡眠",
    "健身",
    "防晒",
    "保温",
    "饮水",
    "背包",
    "雨衣",
    "汽车",
    "燃油车",
    "电动车",
    "自行车",
    "服装",
    "鞋",
    "食品",
    "饮料",
    "商品",
    "购买",
    "零售",
    " apartment",
    "household",
    "consumer",
    "storage",
    "organizer",
    "kitchen",
    "cleaning",
    "outdoor",
    "camping",
    "commute",
    "pet owner",
    "baby",
    "senior",
    "sleep",
    "fitness",
    "sunscreen",
    "hydration",
    "backpack",
    "rainwear",
    "electric vehicle",
    "retail",
    "buyers",
)


@dataclass(frozen=True, slots=True)
class ResearchScreeningDecision:
    event_id: int
    input_hash: str
    decision: str
    reason_codes: list[str]
    explanation: str
    signals: dict[str, Any]
    version: str = SCREENING_VERSION

    @property
    def allows_deep_research(self) -> bool:
        return self.decision == "eligible"


def _contains_term(text: str, term: str) -> bool:
    normalized = term.casefold().strip()
    if not normalized:
        return False
    if re.fullmatch(r"[a-z0-9][a-z0-9 .+_-]*", normalized):
        return re.search(
            rf"(?<![a-z0-9]){re.escape(normalized)}(?![a-z0-9])", text
        ) is not None
    return normalized in text


def _matched_terms(text: str, terms: tuple[str, ...]) -> list[str]:
    return [term for term in terms if _contains_term(text, term)]


def _screening_corpus(event: dict, source_items: list[dict]) -> str:
    values = [str(event.get("canonical_title") or "")]
    for item in source_items[:8]:
        values.append(str(item.get("title") or ""))
        raw = item.get("raw")
        if raw is None:
            try:
                raw = json.loads(item.get("raw_json") or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                raw = {}
        if isinstance(raw, dict):
            extra = raw.get("extra") if isinstance(raw.get("extra"), dict) else {}
            values.extend(str(extra.get(key) or "") for key in ("hover", "info"))
    return " ".join(value.strip() for value in values if value).casefold()


def screen_research_event(
    event: dict,
    source_items: list[dict] | None = None,
    *,
    version: str = SCREENING_VERSION,
) -> ResearchScreeningDecision:
    source_items = source_items or []
    corpus = _screening_corpus(event, source_items)
    input_payload = {
        "event_id": int(event.get("id") or 0),
        "title": str(event.get("canonical_title") or ""),
        "market": str(event.get("market") or ""),
        "signal_type": str(event.get("signal_type") or ""),
        "corpus": corpus,
    }
    input_hash = hashlib.sha256(
        json.dumps(
            input_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()

    exclusion_hits = {
        code: _matched_terms(corpus, terms)
        for code, terms in EXCLUSION_RULES.items()
    }
    exclusion_hits = {code: hits for code, hits in exclusion_hits.items() if hits}
    durability_hits = _matched_terms(corpus, DURABILITY_TERMS)
    physical_hits = _matched_terms(corpus, PHYSICAL_CONSUMPTION_TERMS)
    one_off_hits = _matched_terms(corpus, ONE_OFF_EVENT_TERMS)
    signals = {
        "excluded_topics": exclusion_hits,
        "physical_consumption_terms": physical_hits,
        "durability_terms": durability_hits,
        "one_off_terms": one_off_hits,
    }

    if str(event.get("human_label") or "") == "high_risk":
        return ResearchScreeningDecision(
            int(event.get("id") or 0),
            input_hash,
            "rejected",
            ["human_high_risk"],
            "人工标签已标记为高风险，不进入实体选品研究。",
            signals,
            version,
        )
    if exclusion_hits:
        codes = list(exclusion_hits)
        labels = "、".join(codes)
        return ResearchScreeningDecision(
            int(event.get("id") or 0),
            input_hash,
            "rejected",
            codes,
            f"话题命中排除类型（{labels}），不适合第一阶段低风险实体选品。",
            signals,
            version,
        )
    if one_off_hits and not durability_hits:
        return ResearchScreeningDecision(
            int(event.get("id") or 0),
            input_hash,
            "rejected",
            ["one_off_or_lead_time_mismatch"],
            "话题表现为一次性或即时事件，持续时间不足以覆盖商品开发、生产和运输周期。",
            signals,
            version,
        )
    if not physical_hits:
        return ResearchScreeningDecision(
            int(event.get("id") or 0),
            input_hash,
            "needs_review",
            ["physical_consumption_link_unclear"],
            "标题和来源摘要尚不能描述实体消费用户、场景或约束；暂不投入正文抓取预算。",
            signals,
            version,
        )

    reason_codes = ["physical_consumption_link_found"]
    if durability_hits:
        reason_codes.append("durability_signal_found")
        explanation = "已发现实体消费场景和持续性信号，可以投入有限的正文核实预算。"
    else:
        reason_codes.append("durability_requires_evidence")
        explanation = "已发现实体消费场景，但持续性仍需正文核实；只投入默认的小额抓取预算。"
    return ResearchScreeningDecision(
        int(event.get("id") or 0),
        input_hash,
        "eligible",
        reason_codes,
        explanation,
        signals,
        version,
    )


def persist_research_screening(
    db: Database, decision: ResearchScreeningDecision
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        """INSERT OR IGNORE INTO research_screenings
        (event_id,input_hash,decision,reason_codes_json,explanation,signals_json,
         version,created_at)
        VALUES(?,?,?,?,?,?,?,?)""",
        (
            decision.event_id,
            decision.input_hash,
            decision.decision,
            db.json(decision.reason_codes),
            decision.explanation,
            db.json(decision.signals),
            decision.version,
            now,
        ),
    )
    row = db.one(
        """SELECT * FROM research_screenings
        WHERE event_id=? AND input_hash=? AND version=?""",
        (decision.event_id, decision.input_hash, decision.version),
    )
    if row is None:
        raise RuntimeError("failed to persist research screening")
    return decode_research_screening(row)


def decode_research_screening(row: dict[str, Any]) -> dict[str, Any]:
    decoded = dict(row)
    decoded["reason_codes"] = json.loads(decoded.pop("reason_codes_json") or "[]")
    decoded["signals"] = json.loads(decoded.pop("signals_json") or "{}")
    return decoded


def decode_screening_review(row: dict[str, Any]) -> dict[str, Any]:
    return dict(row)


def pending_screening_review_rows(db: Database, limit: int = 100) -> list[dict[str, Any]]:
    """Return only the latest unresolved needs-review decision for each event."""
    rows = db.all(
        """SELECT s.*,e.canonical_title event_title,e.market,e.language,e.signal_type
        FROM research_screenings s
        JOIN trend_events e ON e.id=s.event_id
        LEFT JOIN research_screening_reviews r ON r.screening_id=s.id
        WHERE s.decision='needs_review' AND r.id IS NULL
          AND s.id=(SELECT MAX(latest.id) FROM research_screenings latest
                    WHERE latest.event_id=s.event_id)
        ORDER BY s.id DESC LIMIT ?""",
        (limit,),
    )
    return [decode_research_screening(row) for row in rows]


def record_screening_review(
    db: Database,
    screening_id: int,
    decision: str,
    note: str = "",
) -> tuple[dict[str, Any], bool]:
    """Persist one immutable human decision; identical retries are idempotent."""
    if decision not in SCREENING_REVIEW_DECISIONS:
        raise ValueError("无效的初筛复核决定")
    screening = db.one("SELECT * FROM research_screenings WHERE id=?", (screening_id,))
    if screening is None:
        raise LookupError("未找到初筛记录")
    if screening["decision"] != "needs_review":
        raise ValueError("只有待复核初筛可以人工处理")
    latest = db.one(
        "SELECT id FROM research_screenings WHERE event_id=? ORDER BY id DESC LIMIT 1",
        (screening["event_id"],),
    )
    if latest is None or int(latest["id"]) != screening_id:
        raise ValueError("只能处理该事件最新的初筛记录")
    existing = db.one(
        "SELECT * FROM research_screening_reviews WHERE screening_id=?",
        (screening_id,),
    )
    if existing is not None:
        if existing["decision"] != decision:
            raise ValueError("初筛复核决定不可改写")
        return decode_screening_review(existing), False
    inserted_id = db.execute(
        """INSERT OR IGNORE INTO research_screening_reviews
        (screening_id,event_id,decision,note,created_at)
        VALUES(?,?,?,?,?)""",
        (
            screening_id,
            screening["event_id"],
            decision,
            note.strip(),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    saved = db.one(
        "SELECT * FROM research_screening_reviews WHERE screening_id=?",
        (screening_id,),
    )
    if saved is None:
        raise RuntimeError("初筛复核记录保存失败")
    if saved["decision"] != decision:
        raise ValueError("初筛复核决定不可改写")
    return decode_screening_review(saved), inserted_id > 0


def rescreen_pending_research_candidates(db: Database) -> dict[str, int]:
    """Apply the current cheap gate to legacy, not-yet-reviewed queue entries."""
    candidates = db.all(
        """SELECT id,event_id,status FROM research_candidates
        WHERE status IN ('pending','evidence_ready','insufficient_evidence','failed')
        ORDER BY id"""
    )
    checked = 0
    superseded = 0
    for candidate in candidates:
        event = db.one(
            "SELECT * FROM trend_events WHERE id=?", (candidate["event_id"],)
        )
        if event is None:
            continue
        source_items = db.all(
            """SELECT i.* FROM source_items i
            JOIN event_members m ON m.source_item_id=i.id
            WHERE m.event_id=? ORDER BY i.rank LIMIT 8""",
            (candidate["event_id"],),
        )
        decision = screen_research_event(event, source_items)
        persist_research_screening(db, decision)
        checked += 1
        if decision.allows_deep_research:
            continue
        db.execute(
            """UPDATE research_candidates SET status='superseded',updated_at=?
            WHERE id=? AND status IN
            ('pending','evidence_ready','insufficient_evidence','failed')""",
            (datetime.now(timezone.utc).isoformat(), candidate["id"]),
        )
        superseded += 1
    return {"checked": checked, "superseded": superseded}


def screening_as_dict(decision: ResearchScreeningDecision) -> dict[str, Any]:
    return asdict(decision)
