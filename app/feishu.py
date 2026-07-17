from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from .config import Settings


@dataclass(slots=True)
class DeliveryResult:
    success: bool
    status_code: int | None
    response_excerpt: str
    error: str | None = None


def _signature(secret: str, timestamp: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(
        string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("utf-8")


async def send_opportunity(
    settings: Settings,
    opportunity: dict[str, Any],
    event: dict[str, Any],
) -> DeliveryResult:
    if not settings.feishu_webhook_url:
        return DeliveryResult(False, None, "", "FEISHU_WEBHOOK_URL is not configured")
    timestamp = str(int(time.time()))
    detail_url = f"{settings.public_base_url}/events/{event['id']}"
    content = (
        f"**{opportunity['name']}**\n"
        f"事件：{event['canonical_title']}\n"
        f"信号来源：{event.get('market', 'CN')} · 目标站点：{opportunity.get('marketplace') or '待确认站点'}"
        f" ({opportunity.get('target_marketplace') or '待确认'})\n"
        f"目标人群：{opportunity['target_segment']}\n"
        f"产品方向：{opportunity['solution']}\n"
        f"已验证推荐分：{opportunity['validated_recommendation_score']} / 100\n"
        f"证据置信度：{opportunity['evidence_confidence']} / 100\n"
        f"[查看证据与评分]({detail_url})"
    )
    payload: dict[str, Any] = {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": "已验证选品推荐"}},
            "body": {"elements": [{"tag": "markdown", "content": content}]},
        },
    }
    if settings.feishu_secret:
        payload["timestamp"] = timestamp
        payload["sign"] = _signature(settings.feishu_secret, timestamp)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(settings.feishu_webhook_url, json=payload)
        excerpt = response.text[:500]
        success = response.status_code == 200
        if success:
            try:
                body = response.json()
                if "code" in body:
                    success = body["code"] == 0
                elif "StatusCode" in body:
                    success = body["StatusCode"] == 0
                else:
                    success = False
            except json.JSONDecodeError:
                success = False
        return DeliveryResult(
            success=success,
            status_code=response.status_code,
            response_excerpt=excerpt,
            error=None if success else "Feishu rejected the message",
        )
    except Exception as exc:
        return DeliveryResult(False, None, "", f"{type(exc).__name__}: {str(exc)[:300]}")


def _digest_section(title: str, signals: list[dict[str, Any]]) -> str:
    lines = [f"### {title}"]
    if not signals:
        return "\n".join([*lines, "今日没有趋势事件。"])
    for index, item in enumerate(signals, 1):
        lines.extend(
            [
                f"**{index}. {item['event_title']}｜趋势分 {item['trend_score']}**",
                f"市场 {item.get('market', '')} · 类型 {item.get('signal_type', '')}"
                f" · {item.get('source_count', 0)} 个来源",
                f"[查看详情]({item.get('detail_url', '')})",
            ]
        )
    return "\n".join(lines)


async def send_daily_digest(
    settings: Settings, digest: dict[str, Any]
) -> DeliveryResult:
    if not settings.feishu_webhook_url:
        return DeliveryResult(False, None, "", "FEISHU_WEBHOOK_URL is not configured")
    for group in (digest["cn_top3"], digest["overseas_top3"]):
        for item in group:
            item["detail_url"] = f"{settings.public_base_url}/events/{item['event_id']}"
    content = "\n\n".join(
        [
            f"数据日期：{digest['date']}。以下仅为事实层趋势事件，不是商品或选品推荐。",
            _digest_section("中国趋势信号 Top 3", digest["cn_top3"]),
            _digest_section("海外趋势信号 Top 3", digest["overseas_top3"]),
            "注：趋势热度不等于消费需求、市场销量或商业成功率。",
        ]
    )
    payload: dict[str, Any] = {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": "每日全球趋势信号摘要"}},
            "body": {"elements": [{"tag": "markdown", "content": content}]},
        },
    }
    timestamp = str(int(time.time()))
    if settings.feishu_secret:
        payload["timestamp"] = timestamp
        payload["sign"] = _signature(settings.feishu_secret, timestamp)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(settings.feishu_webhook_url, json=payload)
        excerpt = response.text[:500]
        success = response.status_code == 200
        if success:
            try:
                body = response.json()
                success = body.get("code", body.get("StatusCode")) == 0
            except json.JSONDecodeError:
                success = False
        return DeliveryResult(
            success, response.status_code, excerpt,
            None if success else "Feishu rejected the digest",
        )
    except Exception as exc:
        return DeliveryResult(False, None, "", f"{type(exc).__name__}: {str(exc)[:300]}")


def delivery_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
