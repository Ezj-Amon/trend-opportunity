from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RiskFlag:
    category: str
    severity: str
    reason: str


BLOCKING_RULES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "ip",
        ("官方同款", "明星同款", "赛事周边", "影视周边", "品牌仿", "logo复刻", "counterfeit"),
        "疑似依赖品牌、人物、赛事或影视 IP，未经权利核验不得推进",
    ),
    (
        "compliance",
        ("治愈", "治疗", "药效", "减肥保证", "防癌", "cure", "treat disease"),
        "包含高风险医疗或功效宣称",
    ),
)

HIGH_RULES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "compliance",
        ("婴儿", "儿童食品", "食品接触", "医疗器械", "supplement"),
        "涉及儿童、食品接触、医疗器械或补充剂，需要专项合规确认",
    ),
    (
        "logistics",
        ("锂电", "电池", "液体", "喷雾", "易燃", "battery", "aerosol"),
        "可能属于电池、液体、喷雾或危险品物流",
    ),
)

MEDIUM_RULES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (
        "seasonality",
        ("台风", "暴雨", "高温", "圣诞", "万圣节", "heatwave", "storm", "christmas"),
        "季节或事件窗口可能短于打样、备货和运输周期",
    ),
    (
        "supply_chain",
        ("定制", "智能", "电子", "家具", "custom", "smart", "electronic", "furniture"),
        "需要确认 MOQ、交期、质量一致性和售后复杂度",
    ),
)


def assess_product_risk(
    event: dict[str, Any], draft: Any
) -> tuple[str, list[dict[str, str]]]:
    corpus = " ".join(
        [
            str(event.get("canonical_title", "")),
            str(getattr(draft, "name", "")),
            str(getattr(draft, "solution", "")),
            str(getattr(draft, "mvp", "")),
            " ".join(getattr(draft, "risks", []) or []),
        ]
    ).casefold()
    flags: list[RiskFlag] = []
    for severity, rules in (
        ("blocking", BLOCKING_RULES),
        ("high", HIGH_RULES),
        ("medium", MEDIUM_RULES),
    ):
        for category, keywords, reason in rules:
            if any(keyword.casefold() in corpus for keyword in keywords):
                flags.append(RiskFlag(category, severity, reason))

    if not flags:
        flags.append(
            RiskFlag(
                "economics",
                "low",
                "仍需补齐采购、头程、平台费、广告和退货成本后确认贡献毛利",
            )
        )
    order = {"low": 0, "medium": 1, "high": 2, "blocking": 3}
    level = max((flag.severity for flag in flags), key=order.__getitem__)
    return level, [
        {"category": flag.category, "severity": flag.severity, "reason": flag.reason}
        for flag in flags
    ]
