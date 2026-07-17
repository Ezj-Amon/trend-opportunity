from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from typing import Any

from .amazon import (
    is_search_term_ready,
    is_supported_marketplace,
    marketplace_code,
    normalize_search_term,
)
from .market_validation import MarketScores, MarketValidationInput


SCORE_FIELDS = tuple(MarketScores.model_fields)
TEXT_METRIC_FIELDS = (
    "niche",
    "hot_search_terms",
    "price_range_360d",
    "top_clicked_brands",
    "top_clicked_categories",
    "top1_asin",
    "top1_product_name",
    "top2_asin",
    "top2_product_name",
    "top3_asin",
    "top3_product_name",
)
NUMERIC_METRIC_FIELDS = (
    "top_clicked_product_count",
    "search_volume_360d",
    "search_volume_growth_180d_pct",
    "search_volume_90d",
    "search_volume_growth_90d_pct",
    "units_sold_360d",
    "average_units_sold_360d",
    "return_rate_360d_pct",
    "average_price_360d",
    "top1_click_share_pct",
    "top1_conversion_share_pct",
    "top2_click_share_pct",
    "top2_conversion_share_pct",
    "top3_click_share_pct",
    "top3_conversion_share_pct",
    "search_volume",
    "search_growth_pct",
    "product_count",
    "median_price",
    "median_rating",
    "median_review_count",
    "top_products_click_share_pct",
    "top_products_purchase_share_pct",
    "return_rate_pct",
    "search_frequency_rank",
    "click_share_pct",
    "conversion_share_pct",
    "landed_cost",
    "estimated_amazon_fees",
    "estimated_ad_cost_pct",
    "estimated_gross_margin_pct",
)
METRIC_FIELDS = (*TEXT_METRIC_FIELDS, *NUMERIC_METRIC_FIELDS)
TEMPLATE_COLUMNS = (
    "opportunity_id",
    "target_marketplace",
    "provider",
    "collected_at",
    "search_term",
    *METRIC_FIELDS,
    *SCORE_FIELDS,
    "source",
    "note",
)
COLUMN_LABELS = {
    "opportunity_id": "机会ID",
    "target_marketplace": "目标站点",
    "provider": "数据来源",
    "collected_at": "采集日期",
    "search_term": "查询词",
    "niche": "细分市场",
    "hot_search_terms": "热门搜索词",
    "top_clicked_product_count": "点击量最多的商品数量",
    "search_volume_360d": "搜索量总计（过去360天）",
    "search_volume_growth_180d_pct": "搜索量增长（过去180天）%",
    "search_volume_90d": "搜索量总计（最近90天）",
    "search_volume_growth_90d_pct": "搜索量增长（最近90天）%",
    "units_sold_360d": "售出商品数量（过去360天）",
    "average_units_sold_360d": "平均销售商品数量（过去360天）",
    "return_rate_360d_pct": "退货率（过去360天）%",
    "average_price_360d": "平均价格（过去360天）",
    "price_range_360d": "价格范围（过去360天）",
    "search_frequency_rank": "搜索频率排名",
    "top_clicked_brands": "点击量最高的品牌",
    "top_clicked_categories": "点击量最高的分类",
    "top1_asin": "商品1 ASIN",
    "top1_product_name": "商品1 商品名称",
    "top1_click_share_pct": "商品1 点击占比%",
    "top1_conversion_share_pct": "商品1 转化率份额%",
    "top2_asin": "商品2 ASIN",
    "top2_product_name": "商品2 商品名称",
    "top2_click_share_pct": "商品2 点击占比%",
    "top2_conversion_share_pct": "商品2 转化率份额%",
    "top3_asin": "商品3 ASIN",
    "top3_product_name": "商品3 商品名称",
    "top3_click_share_pct": "商品3 点击占比%",
    "top3_conversion_share_pct": "商品3 转化率份额%",
    "landed_cost": "到岸成本",
    "estimated_amazon_fees": "预估Amazon费用",
    "estimated_ad_cost_pct": "预估广告成本率%",
    "estimated_gross_margin_pct": "预估毛利率%",
    "search_demand_score": "搜索需求评分",
    "purchase_intent_score": "购买意图评分",
    "competition_score": "竞争机会评分",
    "unit_economics_score": "单位经济性评分",
    "differentiation_score": "差异化评分",
    "execution_score": "执行可行性评分",
    "timing_score": "时机持续性评分",
    "evidence_score": "证据完整度评分",
    "source": "来源文件",
    "note": "备注",
}
LABEL_COLUMNS = tuple(COLUMN_LABELS.get(field, field) for field in TEMPLATE_COLUMNS)
LABEL_TO_COLUMN = {label: field for field, label in COLUMN_LABELS.items()}
ALLOWED_PROVIDERS = {
    "amazon-product-opportunity-explorer",
    "amazon-brand-analytics",
    "amazon-first-party-manual",
    "amazon-first-party-bundle",
}


@dataclass(frozen=True, slots=True)
class ParsedValidationRow:
    line_number: int
    opportunity_id: int
    target_marketplace: str
    value: MarketValidationInput


@dataclass(frozen=True, slots=True)
class RawSourceResult:
    source: str
    query_term: str
    matched_term: str
    metrics: dict[str, Any]
    rows_scanned: int


def _clean_term(value: str | None) -> str:
    return normalize_search_term((value or "").replace("\ufffc", ""))


def _raw_number(value: str | None, field: str) -> float | None:
    raw = (value or "").strip().lstrip("'").replace(",", "")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{field} 不是有效数字：{value}") from exc


def _integer(value: str | None, field: str) -> int | None:
    number = _raw_number(value, field)
    return None if number is None else int(number)


def _percentage_fraction(value: str | None, field: str) -> float | None:
    number = _raw_number(value, field)
    return None if number is None else round(number * 100, 4)


def _find_header_reader(
    content: str, required_columns: set[str], source_name: str
) -> tuple[csv.DictReader, list[str]]:
    stream = io.StringIO(content.lstrip("\ufeff"))
    leading_lines: list[str] = []
    for line_number, line in enumerate(stream, start=1):
        columns = next(csv.reader([line]))
        if required_columns.issubset(set(columns)):
            return csv.DictReader(stream, fieldnames=columns), leading_lines
        leading_lines.append(line.rstrip("\r\n"))
        if line_number >= 10:
            break
    raise ValueError(f"无法在 {source_name} 前 10 行找到所需表头")


def parse_product_opportunity_explorer_csv(
    content: str, search_term: str
) -> RawSourceResult:
    source = "商机探测器"
    reader, metadata = _find_header_reader(
        content,
        {"细分市场", "搜索量（过去 360 天内）", "搜索量（过去 90 天内）"},
        source,
    )
    target = _clean_term(search_term).casefold()
    matched: dict[str, str] | None = None
    row_count = 0
    for row in reader:
        if not any((value or "").strip() for value in row.values()):
            continue
        row_count += 1
        niche = _clean_term(row.get("细分市场"))
        hot_terms = [
            _clean_term(row.get(f"热门搜索词 {index}")) for index in range(1, 4)
        ]
        if niche.casefold() == target or target in {
            term.casefold() for term in hot_terms if term
        }:
            matched = row
            break
    if not matched:
        raise ValueError(f"商机探测器文件中没有与查询词“{search_term}”匹配的细分市场")

    hot_terms = [
        _clean_term(matched.get(f"热门搜索词 {index}")) for index in range(1, 4)
    ]
    units_low = _integer(
        matched.get("售出商品数量下限（最近 360 天内）"), "售出商品数量下限"
    )
    units_high = _integer(
        matched.get("售出商品数量上限（最近 360 天内）"), "售出商品数量上限"
    )
    average_units_low = _integer(
        matched.get("平均售出商品件数范围下限（最近 360 天内）"),
        "平均售出商品件数范围下限",
    )
    average_units_high = _integer(
        matched.get("平均售出商品件数范围上限（最近 360 天内）"),
        "平均售出商品件数范围上限",
    )
    price_low = _raw_number(
        matched.get("最低价格（过去 360 天内）(USD)"), "最低价格"
    )
    price_high = _raw_number(
        matched.get("最高价格（过去 360 天内）(USD)"), "最高价格"
    )
    metrics: dict[str, Any] = {
        "niche": _clean_term(matched.get("细分市场")),
        "hot_search_terms": [term for term in hot_terms if term],
        "search_volume_360d": _integer(
            matched.get("搜索量（过去 360 天内）"), "搜索量（过去 360 天内）"
        ),
        "search_volume_growth_180d_pct": _percentage_fraction(
            matched.get("搜索量增长（过去 180 天）"), "搜索量增长（过去 180 天）"
        ),
        "search_volume_90d": _integer(
            matched.get("搜索量（过去 90 天内）"), "搜索量（过去 90 天内）"
        ),
        "search_volume_growth_90d_pct": _percentage_fraction(
            matched.get("搜索量增长（过去 90 天内）"), "搜索量增长（过去 90 天内）"
        ),
        "units_sold_360d_low": units_low,
        "units_sold_360d_high": units_high,
        "units_sold_360d": (
            round((units_low + units_high) / 2) if units_low is not None and units_high is not None else None
        ),
        "average_units_sold_360d_low": average_units_low,
        "average_units_sold_360d_high": average_units_high,
        "average_units_sold_360d": (
            round((average_units_low + average_units_high) / 2)
            if average_units_low is not None and average_units_high is not None
            else None
        ),
        "top_clicked_product_count": _integer(
            matched.get("点击量最多的商品数量"), "点击量最多的商品数量"
        ),
        "average_price_360d": _raw_number(matched.get("平均价格 (USD)"), "平均价格"),
        "price_low_360d": price_low,
        "price_high_360d": price_high,
        "price_range_360d": (
            f"${price_low:g}-${price_high:g}"
            if price_low is not None and price_high is not None
            else ""
        ),
        "return_rate_360d_pct": _percentage_fraction(
            matched.get("退货率 (过去 360 天)"), "退货率"
        ),
        "source_metadata": metadata,
    }
    return RawSourceResult(
        source=source,
        query_term=search_term,
        matched_term=metrics["niche"],
        metrics={key: value for key, value in metrics.items() if value is not None},
        rows_scanned=row_count,
    )


def parse_brand_analytics_hot_terms_csv(
    content: str, search_term: str
) -> RawSourceResult:
    source = "品牌分析-热门搜索词"
    reader, metadata = _find_header_reader(
        content, {"搜索频率排名", "搜索词", "报告日期"}, source
    )
    target = _clean_term(search_term).casefold()
    matched: dict[str, str] | None = None
    row_count = 0
    for row in reader:
        if not any((value or "").strip() for value in row.values()):
            continue
        row_count += 1
        if _clean_term(row.get("搜索词")).casefold() == target:
            matched = row
            break
    if not matched:
        raise ValueError(f"热门搜索词文件中没有精确查询词“{search_term}”")

    metrics: dict[str, Any] = {
        "search_frequency_rank": _integer(matched.get("搜索频率排名"), "搜索频率排名"),
        "top_clicked_brands": [
            value
            for value in (
                matched.get("点击量第1的品牌"),
                matched.get("点击量第2的品牌"),
                matched.get("点击量第3的品牌"),
            )
            if value
        ],
        "top_clicked_categories": [
            value
            for value in (
                matched.get("点击量最高的分类"),
                matched.get("点击量第二的分类"),
                matched.get("点击量第 3 的分类"),
            )
            if value
        ],
        "report_date": (matched.get("报告日期") or "").strip(),
        "source_metadata": metadata,
    }
    column_sets = {
        1: (
            "点击量第1的商品：ASIN",
            "点击量第1的商品: 商品标题",
            "点击量最高的商品：点击份额",
            "点击量第1的商品：转化贡献占比",
        ),
        2: (
            "点击量第2的商品：ASIN",
            "点击量第2的商品：商品标题",
            "点击量第二的商品：点击份额",
            "热门点击商品第2名：转化贡献占比",
        ),
        3: (
            "点击量第三的商品：ASIN",
            "点击量第3的商品：商品标题",
            "点击量第三的商品：点击份额",
            "点击量第3的商品：转化贡献占比",
        ),
    }
    for rank, (asin_col, title_col, click_col, conversion_col) in column_sets.items():
        metrics[f"top{rank}_asin"] = (matched.get(asin_col) or "").strip()
        metrics[f"top{rank}_product_name"] = (matched.get(title_col) or "").strip()
        metrics[f"top{rank}_click_share_pct"] = _raw_number(
            matched.get(click_col), click_col
        )
        metrics[f"top{rank}_conversion_share_pct"] = _raw_number(
            matched.get(conversion_col), conversion_col
        )
    return RawSourceResult(
        source=source,
        query_term=search_term,
        matched_term=_clean_term(matched.get("搜索词")),
        metrics={
            key: value
            for key, value in metrics.items()
            if value is not None and value != ""
        },
        rows_scanned=row_count,
    )


def _bucket(value: float, thresholds: tuple[float, float, float, float]) -> int:
    if value >= thresholds[3]:
        return 5
    if value >= thresholds[2]:
        return 4
    if value >= thresholds[1]:
        return 3
    if value >= thresholds[0]:
        return 2
    return 1


def build_raw_amazon_validation(
    *,
    opportunity_id: int,
    marketplace: str,
    search_term: str,
    product_opportunity_csv: str,
    hot_search_terms_csv: str,
) -> MarketValidationInput:
    if not is_search_term_ready(search_term, marketplace):
        raise ValueError("机会没有可用于美国站的具体英文商品查询词")
    poe = parse_product_opportunity_explorer_csv(product_opportunity_csv, search_term)
    hot = parse_brand_analytics_hot_terms_csv(hot_search_terms_csv, search_term)
    metrics = {**poe.metrics, **hot.metrics}

    search_volume_90d = float(metrics.get("search_volume_90d") or 0)
    search_volume_360d = float(metrics.get("search_volume_360d") or 0)
    units_sold = float(metrics.get("units_sold_360d") or 0)
    sales_to_search = units_sold / search_volume_360d if search_volume_360d else 0
    top_click_total = sum(
        float(metrics.get(f"top{rank}_click_share_pct") or 0) for rank in range(1, 4)
    )
    top_conversion_total = sum(
        float(metrics.get(f"top{rank}_conversion_share_pct") or 0)
        for rank in range(1, 4)
    )
    unmet_gap = max(0.0, top_click_total - top_conversion_total)
    growth_90d = float(metrics.get("search_volume_growth_90d_pct") or 0)
    growth_180d = float(metrics.get("search_volume_growth_180d_pct") or 0)

    search_score = _bucket(search_volume_90d, (10_000, 50_000, 250_000, 1_000_000))
    intent_score = _bucket(sales_to_search, (0.003, 0.008, 0.02, 0.05))
    competition_score = 5 if top_click_total <= 25 else 4 if top_click_total <= 40 else 3 if top_click_total <= 55 else 2 if top_click_total <= 70 else 1
    differentiation_score = _bucket(unmet_gap, (2, 5, 10, 15))
    if growth_90d >= 20 and growth_180d >= 20:
        timing_score = 5
    elif growth_90d >= 20 and growth_180d > 0:
        timing_score = 4
    elif growth_90d >= 10:
        timing_score = 3
    elif growth_90d >= 0:
        timing_score = 2
    else:
        timing_score = 1

    explanations = {
        "search_demand_score": f"最近90天搜索量 {search_volume_90d:,.0f}",
        "purchase_intent_score": f"过去360天售出量中点/搜索量约 {sales_to_search:.2%}",
        "competition_score": f"热门搜索词前三商品点击占比合计 {top_click_total:.2f}%",
        "differentiation_score": f"前三商品点击占比与转化份额差值 {unmet_gap:.2f} 个百分点",
        "timing_score": f"90天增长 {growth_90d:.2f}%，180天增长 {growth_180d:.2f}%",
        "evidence_score": "商机探测器与品牌分析热门搜索词均精确匹配",
        "unit_economics_score": "缺少到岸成本、Amazon费用和广告成本",
        "execution_score": "缺少供应链、合规和交付验证",
    }
    metrics.update(
        {
            "sales_to_search_ratio": round(sales_to_search, 6),
            "top3_click_share_pct": round(top_click_total, 4),
            "top3_conversion_share_pct": round(top_conversion_total, 4),
            "unmet_demand_gap_pct_points": round(unmet_gap, 4),
            "score_explanations": explanations,
            "source_rows_scanned": {
                poe.source: poe.rows_scanned,
                hot.source: hot.rows_scanned,
            },
            "source_hashes": {
                poe.source: hashlib.sha256(product_opportunity_csv.encode("utf-8")).hexdigest(),
                hot.source: hashlib.sha256(hot_search_terms_csv.encode("utf-8")).hexdigest(),
            },
        }
    )
    return MarketValidationInput(
        provider="amazon-first-party-bundle",
        provider_version="seller-central-zh-csv-v1",
        marketplace=marketplace,
        query={
            "opportunity_id": opportunity_id,
            "target_marketplace": marketplace,
            "search_term": search_term,
            "matches": {poe.source: poe.matched_term, hot.source: hot.matched_term},
        },
        scores=MarketScores(
            search_demand_score=search_score,
            purchase_intent_score=intent_score,
            competition_score=competition_score,
            unit_economics_score=None,
            differentiation_score=differentiation_score,
            execution_score=None,
            timing_score=timing_score,
            evidence_score=5,
        ),
        metrics=metrics,
        sources=["Seller Central 商机探测器", "Seller Central 品牌分析-热门搜索词"],
        note="由美国站中文 Seller Central 原始 CSV 自动匹配并评分；单位经济性和执行可行性仍需人工补充",
    )


def _number(value: str, field: str, line_number: int) -> int | float | str:
    value = value.strip()
    if not value:
        return ""
    try:
        number = float(value.replace(",", ""))
    except ValueError as exc:
        raise ValueError(f"第 {line_number} 行 {field} 必须是数字") from exc
    return int(number) if number.is_integer() else number


def parse_validation_csv(content: str) -> list[ParsedValidationRow]:
    reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")))
    if not reader.fieldnames:
        raise ValueError("CSV 没有表头")
    canonical_fields = {
        LABEL_TO_COLUMN.get((field or "").strip(), (field or "").strip())
        for field in reader.fieldnames
    }
    missing_columns = {"opportunity_id", *SCORE_FIELDS} - canonical_fields
    if missing_columns:
        raise ValueError(f"CSV 缺少列：{', '.join(sorted(missing_columns))}")

    parsed: list[ParsedValidationRow] = []
    for line_number, raw_row in enumerate(reader, start=2):
        row = {
            LABEL_TO_COLUMN.get((key or "").strip(), (key or "").strip()): value
            for key, value in raw_row.items()
        }
        if not any((value or "").strip() for value in row.values()):
            continue
        try:
            opportunity_id = int((row.get("opportunity_id") or "").strip())
        except ValueError as exc:
            raise ValueError(f"第 {line_number} 行 opportunity_id 必须是整数") from exc
        target_marketplace = marketplace_code(row.get("target_marketplace") or "US")
        if not is_supported_marketplace(target_marketplace):
            raise ValueError(f"第 {line_number} 行不支持站点 {target_marketplace}")
        provider = (row.get("provider") or "amazon-first-party-manual").strip()
        if provider not in ALLOWED_PROVIDERS:
            raise ValueError(f"第 {line_number} 行 provider 不受支持：{provider}")

        scores: dict[str, int | None] = {}
        for field in SCORE_FIELDS:
            raw = (row.get(field) or "").strip()
            if not raw:
                scores[field] = None
                continue
            try:
                score = int(raw)
            except ValueError as exc:
                raise ValueError(f"第 {line_number} 行 {field} 必须是 1-5 的整数") from exc
            if score < 1 or score > 5:
                raise ValueError(f"第 {line_number} 行 {field} 必须是 1-5 的整数")
            scores[field] = score
        if all(score is None for score in scores.values()):
            raise ValueError(f"第 {line_number} 行至少填写一个评分")

        metrics: dict[str, Any] = {}
        for field in TEXT_METRIC_FIELDS:
            raw = (row.get(field) or "").strip()
            if raw:
                metrics[field] = raw
        for field in NUMERIC_METRIC_FIELDS:
            raw = row.get(field) or ""
            value = _number(raw, field, line_number)
            if value != "":
                metrics[field] = value
        collected_at = (row.get("collected_at") or "").strip()
        if collected_at:
            metrics["collected_at"] = collected_at
        source = (row.get("source") or "").strip()
        search_term = (row.get("search_term") or "").strip()
        value = MarketValidationInput(
            provider=provider,
            provider_version="normalized-csv-v1",
            marketplace=target_marketplace,
            query={"target_marketplace": target_marketplace, "search_term": search_term},
            scores=MarketScores(**scores),
            metrics=metrics,
            sources=[part.strip() for part in source.split("|") if part.strip()],
            note=(row.get("note") or "").strip(),
        )
        parsed.append(
            ParsedValidationRow(line_number, opportunity_id, target_marketplace, value)
        )
        if len(parsed) > 500:
            raise ValueError("单次最多导入 500 行")
    if not parsed:
        raise ValueError("CSV 没有可导入的数据行")
    return parsed


def _spreadsheet_safe(value: str) -> str:
    if value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def build_template(rows: list[dict[str, Any]]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=LABEL_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        values = {
                "opportunity_id": row.get("id", ""),
                "target_marketplace": row.get("target_marketplace", "US"),
                "provider": "amazon-product-opportunity-explorer",
                "search_term": _spreadsheet_safe(
                    str(row.get("amazon_search_term") or "")
                ),
                "source": "Seller Central export",
            }
        writer.writerow({COLUMN_LABELS.get(key, key): value for key, value in values.items()})
    if not rows:
        values = {
                "target_marketplace": "US",
                "provider": "amazon-product-opportunity-explorer",
                "source": "Seller Central export",
            }
        writer.writerow({COLUMN_LABELS.get(key, key): value for key, value in values.items()})
    return "\ufeff" + stream.getvalue()
