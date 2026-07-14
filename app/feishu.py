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
        f"市场：{event.get('market', 'CN')} · {opportunity.get('marketplace') or '待确认站点'}\n"
        f"目标人群：{opportunity['target_segment']}\n"
        f"产品方向：{opportunity['solution']}\n"
        f"机会分：{opportunity['opportunity_score']} / 100\n"
        f"证据置信度：{opportunity['evidence_confidence']} / 100\n"
        f"[查看证据与评分]({detail_url})"
    )
    payload: dict[str, Any] = {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": "趋势选品机会"}},
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


def delivery_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()
