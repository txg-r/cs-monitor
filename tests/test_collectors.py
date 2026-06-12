import httpx
import pytest

from app.collectors.monitored_page import MonitoredPageCollector
from app.collectors.steam_news import SteamNewsCollector
from app.models import Source


@pytest.mark.asyncio
async def test_monitored_page_first_poll_only_builds_baseline():
    """验证规则页首次采集只建立基线，不把历史规则误判为新变更。"""

    source = Source(id=1, name="规则页", kind="monitored_page", url="https://example.com/rules", config={})
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, text="<html><title>Rules</title><main>" + "交易规则内容 " * 20 + "</main></html>")
    )
    async with httpx.AsyncClient(transport=transport) as client:
        result = await MonitoredPageCollector().collect(source, client)
    assert result.baseline_only is True
    assert result.items == []
    assert result.source_fingerprint
    assert "交易规则内容" in result.source_snapshot


@pytest.mark.asyncio
async def test_monitored_page_emits_item_after_body_change():
    """验证正文指纹变化会形成新证据，同时携带前后指纹供审计。"""

    source = Source(
        id=1,
        name="规则页",
        kind="monitored_page",
        url="https://example.com/rules",
        config={},
        content_fingerprint="old",
        content_snapshot="旧的市场说明。用户可以正常交易。",
    )
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, text="<html><title>Rules</title><main>" + "新增交易限制 " * 20 + "</main></html>")
    )
    async with httpx.AsyncClient(transport=transport) as client:
        result = await MonitoredPageCollector().collect(source, client)
    assert len(result.items) == 1
    assert result.items[0].metadata["previous_fingerprint"] == "old"
    assert "新增交易限制" in result.items[0].body


@pytest.mark.asyncio
async def test_steam_collector_maps_official_response():
    """验证 Steam 稳定 gid、发布时间和正文会被完整映射到公共采集结构。"""

    source = Source(
        id=1,
        name="CS2 官方新闻",
        kind="steam_news",
        url="https://api.example/news",
        config={"appid": 730},
    )
    payload = {
        "appnews": {
            "newsitems": [
                {"gid": "42", "title": "New Case", "url": "https://example.com/42", "contents": "<b>New</b>", "date": 1700000000}
            ]
        }
    }
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json=payload))
    async with httpx.AsyncClient(transport=transport) as client:
        result = await SteamNewsCollector().collect(source, client)
    assert result.items[0].external_id == "42"
    assert result.items[0].body == "New"
