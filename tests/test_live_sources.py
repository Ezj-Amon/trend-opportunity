from __future__ import annotations

import pytest

from app.sources import GoogleTrendsSource, NewsNowSource


@pytest.mark.live
@pytest.mark.asyncio
async def test_real_newsnow_sources_return_current_items() -> None:
    source = NewsNowSource("https://newsnow.busiyi.world", timeout=20)
    results = [await source.fetch(source_id) for source_id in ("weibo", "zhihu", "baidu")]
    failures = [f"{result.source}: {result.error}" for result in results if not result.success]
    assert not failures, failures
    for result in results:
        assert result.status_code == 200
        assert len(result.items) >= 10
        assert all(item.title and item.url.startswith("http") for item in result.items)
        assert result.payload_hash


@pytest.mark.live
@pytest.mark.asyncio
async def test_real_overseas_sources_return_market_metadata() -> None:
    newsnow = NewsNowSource("https://newsnow.busiyi.world", timeout=20)
    results = [
        await newsnow.fetch(source_id)
        for source_id in ("hackernews", "producthunt", "github-trending-today")
    ]
    results.append(await GoogleTrendsSource(timeout=20).fetch("US"))
    failures = [f"{result.source}: {result.error}" for result in results if not result.success]
    assert not failures, failures
    for result in results:
        assert result.status_code == 200
        assert result.items
        assert result.market != "CN"
        assert all(item.market != "CN" and item.signal_type for item in result.items)
