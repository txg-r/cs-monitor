from datetime import datetime, timezone

import httpx

from app.collectors.base import BaseCollector, ConfigurationError
from app.models import Source
from app.schemas import CollectedItem, PollResult


class RedditCollector(BaseCollector):
    """使用已获授权的 Reddit OAuth 应用采集白名单社区。"""

    async def collect(self, source: Source, client: httpx.AsyncClient) -> PollResult:
        config = source.config
        required = ["client_id", "client_secret", "username", "password", "subreddit"]
        if any(not config.get(key) for key in required):
            raise ConfigurationError("Reddit 来源未配置完整 OAuth 密钥，已跳过")

        auth_response = await client.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(config["client_id"], config["client_secret"]),
            data={"grant_type": "password", "username": config["username"], "password": config["password"]},
        )
        auth_response.raise_for_status()
        token = auth_response.json().get("access_token")
        if not token:
            raise ConfigurationError("Reddit OAuth 响应未返回 access_token")

        response = await client.get(
            f"https://oauth.reddit.com/r/{config['subreddit']}/new",
            params={"limit": 50},
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        children = response.json().get("data", {}).get("children", [])
        items = []
        for child in children:
            post = child.get("data", {})
            external_id = post.get("name") or post.get("id")
            if not external_id:
                continue
            items.append(
                CollectedItem(
                    external_id=external_id,
                    url=f"https://www.reddit.com{post.get('permalink', '')}",
                    title=post.get("title") or "Reddit 讨论",
                    body=post.get("selftext") or "",
                    author=post.get("author"),
                    published_at=datetime.fromtimestamp(post.get("created_utc", 0), tz=timezone.utc),
                    metadata={
                        "score": post.get("score", 0),
                        "comments": post.get("num_comments", 0),
                        "subreddit": config["subreddit"],
                        "platform": "reddit",
                    },
                )
            )
        return PollResult(items=items)

