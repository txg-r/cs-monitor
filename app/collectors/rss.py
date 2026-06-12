from datetime import datetime, timezone

import feedparser
import httpx
from dateutil.parser import parse as parse_datetime

from app.collectors.base import BaseCollector, CollectorError
from app.models import Source
from app.schemas import CollectedItem, PollResult
from app.services.text import clean_html


class RssCollector(BaseCollector):
    """采集明确提供 RSS/Atom 的国内外白名单资讯源。"""

    async def collect(self, source: Source, client: httpx.AsyncClient) -> PollResult:
        response = await client.get(source.url)
        response.raise_for_status()
        feed = feedparser.loads(response.content)
        if feed.bozo and not feed.entries:
            raise CollectorError(f"RSS 解析失败: {feed.bozo_exception}")

        items = []
        for entry in feed.entries:
            published_raw = entry.get("published") or entry.get("updated")
            published_at = parse_datetime(published_raw) if published_raw else datetime.now(timezone.utc)
            body = entry.get("content", [{}])[0].get("value") if entry.get("content") else entry.get("summary", "")
            items.append(
                CollectedItem(
                    external_id=str(entry.get("id") or entry.get("link") or entry.get("title")),
                    url=entry.get("link") or source.url,
                    title=entry.get("title") or source.name,
                    body=clean_html(body or ""),
                    author=entry.get("author"),
                    published_at=published_at,
                    metadata={"platform": "rss"},
                )
            )
        return PollResult(items=items)

