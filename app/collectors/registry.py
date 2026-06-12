from app.collectors.base import BaseCollector
from app.collectors.liquipedia import LiquipediaCollector
from app.collectors.monitored_page import MonitoredPageCollector
from app.collectors.reddit import RedditCollector
from app.collectors.rss import RssCollector
from app.collectors.steam_news import SteamNewsCollector
from app.collectors.youtube import YouTubeFeedCollector


COLLECTORS: dict[str, BaseCollector] = {
    "steam_news": SteamNewsCollector(),
    "monitored_page": MonitoredPageCollector(),
    "youtube_feed": YouTubeFeedCollector(),
    "liquipedia": LiquipediaCollector(),
    "reddit": RedditCollector(),
    "rss": RssCollector(),
}


def get_collector(kind: str) -> BaseCollector:
    """按来源类型返回采集器，未知类型立即失败以暴露错误配置。"""

    try:
        return COLLECTORS[kind]
    except KeyError as exc:
        raise ValueError(f"不支持的采集器类型: {kind}") from exc

