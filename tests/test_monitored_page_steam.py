import json

import httpx
import pytest

from app.collectors.monitored_page import MonitoredPageCollector
from app.models import Source
from app.services.pipeline import describe_exception


def _steam_faq_html(title: str, content: str, url_code: str) -> str:
    """构造最小 Steam Support FAQ 壳页面。

    真实页面正文不在静态 DOM，而是藏在 `#application_config[data-faqstore]` 中。
    测试里直接模拟这个结构，避免依赖外网和页面实时变动。
    """

    payload = {
        "faqs": {
            "faq-1": {
                "faq_id": "faq-1",
                "title": title,
                "content": content,
                "url_code": url_code,
            }
        }
    }
    escaped = json.dumps(payload).replace('"', "&quot;")
    return (
        f"<html><head><title>Steam Support :: {title}</title></head>"
        "<body>"
        "<div id='responsive_page_template_content'></div>"
        f"<div id='application_config' data-faqstore=\"{escaped}\"></div>"
        "</body></html>"
    )


@pytest.mark.asyncio
async def test_monitored_page_extracts_steam_support_hidden_faq_store():
    """验证 Steam Support React 壳页能从隐藏 `data-faqstore` 中提取正文，而不是误判成空页面。"""

    source = Source(
        id=2,
        name="Steam 交易和市场限制",
        kind="monitored_page",
        url="https://help.steampowered.com/en/faqs/view/451E-96B3-D194-50FC",
        config={},
    )
    html = _steam_faq_html(
        "Trading and Market Restrictions",
        "[h2]Restrictions[/h2] [list][*]Trade hold applies.[*]VAC bans remove CS2 item trading and drops.[/list]",
        "451E-96B3-D194-50FC",
    )
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=html, request=request))
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        result = await MonitoredPageCollector().collect(source, client)

    assert result.baseline_only is True
    assert "Trading and Market Restrictions" in result.source_snapshot
    assert "Trade hold applies." in result.source_snapshot
    assert "VAC bans remove CS2 item trading and drops." in result.source_snapshot


@pytest.mark.asyncio
async def test_monitored_page_retries_transient_transport_errors():
    """验证规则页请求遇到瞬时连接错误时会自动重试，避免把一次网络抖动直接展示成来源异常。"""

    source = Source(id=3, name="规则页", kind="monitored_page", url="https://example.com/rules", config={})
    html = "<html><title>Rules</title><main>" + ("Updated market rules. " * 12) + "</main></html>"
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise httpx.ConnectError("temporary tls failure", request=request)
        return httpx.Response(200, text=html, request=request)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        result = await MonitoredPageCollector().collect(source, client)

    assert attempts["count"] == 2
    assert result.baseline_only is True
    assert "Updated market rules." in result.source_snapshot


def test_describe_exception_uses_fallback_message_when_exception_is_blank():
    """验证空字符串异常也会被转成可读文案，避免来源状态被错误显示成“等待首次采集”。"""

    assert describe_exception(RuntimeError()) == "RuntimeError：未返回明确错误信息"
