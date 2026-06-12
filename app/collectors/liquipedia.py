from datetime import datetime, timedelta, timezone

import httpx
from dateutil.parser import parse as parse_datetime

from app.collectors.base import BaseCollector, ConfigurationError
from app.models import Source
from app.schemas import CollectedItem, PollResult


LIQUIPEDIA_KEYWORDS = {
    "major", "iem", "blast", "pgl", "esl", "roster", "transfer", "s1mple", "m0nesy", "zywoo", "donk",
}


class LiquipediaCollector(BaseCollector):
    """低频读取 Liquipedia MediaWiki 最近变更。

    这里只筛选赛事和重点人物相关页面，不抓取整站 HTML；这样同时满足 MVP 的高信号目标和
    Liquipedia 对 API、缓存及访问频率的约束。
    """

    async def collect(self, source: Source, client: httpx.AsyncClient) -> PollResult:
        if not source.config.get("contact_email"):
            raise ConfigurationError("Liquipedia 要求可识别 User-Agent，请配置 CONTACT_EMAIL")
        since = datetime.now(timezone.utc) - timedelta(hours=2)
        params = {
            "action": "query",
            "format": "json",
            "list": "recentchanges",
            "rcnamespace": "0",
            "rclimit": "50",
            "rcprop": "title|ids|timestamp|comment|user",
            "rcstart": datetime.now(timezone.utc).isoformat(),
            "rcend": since.isoformat(),
            "formatversion": "2",
        }
        response = await client.get(source.url, params=params)
        response.raise_for_status()
        changes = response.json().get("query", {}).get("recentchanges", [])
        items = []
        for change in changes:
            haystack = f"{change.get('title', '')} {change.get('comment', '')}".lower()
            if not any(keyword in haystack for keyword in LIQUIPEDIA_KEYWORDS):
                continue
            revision_id = change.get("revid") or change.get("rcid")
            title = change.get("title") or "Liquipedia 页面更新"
            items.append(
                CollectedItem(
                    external_id=f"liquipedia:{revision_id}",
                    url=f"https://liquipedia.net/counterstrike/{title.replace(' ', '_')}",
                    title=f"Liquipedia 更新：{title}",
                    body=change.get("comment") or "赛事或阵容页面发生更新",
                    author=change.get("user"),
                    published_at=parse_datetime(change["timestamp"]),
                    metadata={"revision_id": revision_id, "platform": "liquipedia"},
                )
            )
        return PollResult(items=items)

