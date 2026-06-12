from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import Source


def _source_defaults(settings: Settings) -> list[dict]:
    """构造首版默认高信号来源。

    社媒频道依赖用户自己的授权和白名单，因此只在配置完整时创建；官方来源则开箱即用。
    """

    sources = [
        {
            "name": "CS2 官方新闻",
            "kind": "steam_news",
            "url": "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/",
            "credibility": 100,
            "poll_interval_minutes": 5,
            "config": {"appid": 730, "count": 30},
        },
        {
            "name": "Steam 交易和市场限制",
            "kind": "monitored_page",
            "url": "https://help.steampowered.com/en/faqs/view/451E-96B3-D194-50FC",
            "credibility": 100,
            "poll_interval_minutes": 60,
            "config": {},
        },
        {
            "name": "Steam 社区市场 FAQ",
            "kind": "monitored_page",
            "url": "https://steamcommunity.com/market/faq",
            "credibility": 100,
            "poll_interval_minutes": 60,
            "config": {},
        },
        {
            "name": "人工补录",
            "kind": "manual",
            "url": "manual://intake",
            "credibility": 70,
            "poll_interval_minutes": 1440,
            "enabled": False,
            "config": {},
        },
    ]
    if settings.liquipedia_enabled:
        sources.append(
            {
                "name": "Liquipedia CS2 最近变更",
                "kind": "liquipedia",
                "url": settings.liquipedia_api_url,
                "credibility": 75,
                "poll_interval_minutes": max(2, settings.liquipedia_poll_minutes),
                "config": {"contact_email": settings.contact_email},
            }
        )
    for channel_id in settings.youtube_channels:
        sources.append(
            {
                "name": f"YouTube 频道 {channel_id}",
                "kind": "youtube_feed",
                "url": f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}",
                "credibility": 75,
                "poll_interval_minutes": 10,
                "config": {"channel_id": channel_id},
            }
        )
    reddit_ready = all(
        [settings.reddit_client_id, settings.reddit_client_secret, settings.reddit_username, settings.reddit_password]
    )
    if reddit_ready:
        for subreddit in settings.subreddits:
            sources.append(
                {
                    "name": f"Reddit r/{subreddit}",
                    "kind": "reddit",
                    "url": f"https://oauth.reddit.com/r/{subreddit}/new",
                    "credibility": 55,
                    "poll_interval_minutes": 10,
                    "config": {
                        "subreddit": subreddit,
                        "client_id": settings.reddit_client_id,
                        "client_secret": settings.reddit_client_secret,
                        "username": settings.reddit_username,
                        "password": settings.reddit_password,
                    },
                }
            )
    return sources


def seed_sources(db: Session, settings: Settings) -> None:
    """幂等写入默认来源，不覆盖运营人员已经调整过的可信度或轮询频率。"""

    existing_names = set(db.scalars(select(Source.name)).all())
    for data in _source_defaults(settings):
        if data["name"] not in existing_names:
            db.add(Source(**data))
    db.commit()

