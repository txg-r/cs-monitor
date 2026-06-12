from datetime import datetime, timezone

import feedparser
import httpx
from dateutil.parser import parse as parse_datetime

from app.collectors.base import BaseCollector, CollectorError
from app.models import Source
from app.schemas import CollectedItem, PollResult


class YouTubeFeedCollector(BaseCollector):
    """轮询 YouTube 官方 Atom Feed，不消耗关键词搜索配额。"""

    async def collect(self, source: Source, client: httpx.AsyncClient) -> PollResult:
        response = await client.get(source.url)
        response.raise_for_status()
        feed = feedparser.loads(response.content)
        if feed.bozo and not feed.entries:
            raise CollectorError(f"YouTube Feed 解析失败: {feed.bozo_exception}")

        items = []
        for entry in feed.entries:
            video_id = entry.get("yt_videoid") or entry.get("id") or entry.get("link")
            published = entry.get("published") or entry.get("updated")
            published_at = parse_datetime(published) if published else datetime.now(timezone.utc)
            items.append(
                CollectedItem(
                    external_id=str(video_id),
                    url=entry.get("link") or source.url,
                    title=entry.get("title") or "YouTube 新视频",
                    body=entry.get("summary") or "",
                    author=entry.get("author"),
                    published_at=published_at,
                    metadata={"channel_id": source.config.get("channel_id"), "platform": "youtube"},
                )
            )
        return PollResult(items=items)

