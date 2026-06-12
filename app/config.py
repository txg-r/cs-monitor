from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """集中管理运行配置。

    配置统一从环境变量读取，是为了让同一个镜像可以在本机 SQLite、Docker PostgreSQL
    和未来的云环境中复用，避免把密钥或部署差异写入业务代码。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "CS2 饰品趋势情报"
    environment: str = "development"
    database_url: str = "sqlite:///./cs_monitor.db"
    timezone: str = "Asia/Shanghai"
    log_level: str = "INFO"
    log_to_file: bool = True
    log_file_path: str = "logs/app.log"
    log_file_max_bytes: int = 5_000_000
    log_file_backup_count: int = 3
    scheduler_enabled: bool = True
    source_poll_tick_seconds: int = 60
    request_timeout_seconds: float = 20.0
    user_agent: str = "cs-monitor/0.1 (internal research; configure CONTACT_EMAIL)"
    contact_email: str | None = None

    admin_token: str = "change-me"

    ai_enabled: bool = False
    ai_base_url: str = "https://api.openai.com/v1"
    ai_api_key: str | None = None
    ai_model: str = "gpt-4.1-mini"
    ai_timeout_seconds: float = 60.0
    ai_max_retries: int = 2

    feishu_webhook_url: str | None = None
    feishu_secret: str | None = None

    liquipedia_enabled: bool = False
    liquipedia_api_url: str = "https://liquipedia.net/counterstrike/api.php"
    liquipedia_poll_minutes: int = 30

    # 频道 ID 和 subreddit 使用逗号分隔，避免简单部署还要引入配置中心。
    youtube_channel_ids: str = ""
    reddit_subreddits: str = "csgomarketforum,GlobalOffensive"
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_username: str | None = None
    reddit_password: str | None = None

    daily_report_hour: int = Field(default=8, ge=0, le=23)
    daily_report_minute: int = Field(default=30, ge=0, le=59)
    templates_dir: Path = Path(__file__).parent / "templates"

    @property
    def youtube_channels(self) -> list[str]:
        """返回去空、去重后的频道 ID，防止重复配置造成重复采集。"""

        return list(dict.fromkeys(x.strip() for x in self.youtube_channel_ids.split(",") if x.strip()))

    @property
    def subreddits(self) -> list[str]:
        """返回规范化的社区白名单，首版不允许任意全站搜索以控制噪声和合规风险。"""

        return list(dict.fromkeys(x.strip() for x in self.reddit_subreddits.split(",") if x.strip()))


@lru_cache
def get_settings() -> Settings:
    """缓存配置实例，确保整个进程看到一致配置并减少重复解析环境变量。"""

    return Settings()
