from datetime import datetime, timezone

import httpx

from app.collectors.base import BaseCollector, CollectorError
from app.models import Source
from app.schemas import CollectedItem, PollResult
from app.services.text import clean_html


class SteamNewsCollector(BaseCollector):
    """通过 Valve 官方 Web API 获取 CS2 公告。"""

    async def collect(self, source: Source, client: httpx.AsyncClient) -> PollResult:
        params = {"appid": source.config.get("appid", 730), "count": source.config.get("count", 30), "maxlength": 0}
        response = await client.get(source.url, params=params)
        response.raise_for_status()
        payload = response.json()
        news_items = payload.get("appnews", {}).get("newsitems", [])
        if not isinstance(news_items, list):
            raise CollectorError("Steam 新闻响应缺少 appnews.newsitems 数组")

        items = []
        for item in news_items:
            # gid 是 Valve 提供的稳定标识，比标题更适合做幂等键。
            external_id = str(item.get("gid") or item.get("url") or item.get("title"))
            items.append(
                CollectedItem(
                    external_id=external_id,
                    url=item.get("url") or source.url,
                    title=item.get("title") or "CS2 官方公告",
                    body=clean_html(item.get("contents") or item.get("feedlabel") or ""),
                    author=item.get("author"),
                    published_at=datetime.fromtimestamp(int(item.get("date", 0)), tz=timezone.utc),
                    metadata={"feed_name": item.get("feedname"), "feed_label": item.get("feedlabel")},
                )
            )
        return PollResult(items=items)

