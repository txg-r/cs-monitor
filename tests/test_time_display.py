from datetime import datetime

from fastapi.testclient import TestClient

from app.config import Settings
from app.models import Source
from app.services.time import format_local_datetime, to_local_datetime


def test_naive_sqlite_datetime_is_interpreted_as_utc_before_local_display():
    """验证 SQLite 读出的无时区时间会先按 UTC 解释，再转为配置的本地时区。

    这个测试覆盖本次问题的根因：数据库字符串 `2026-06-17 06:13` 实际是 UTC，
    页面直接 strftime 会显示成 06:13，用户在 Asia/Shanghai 本地看到的正确时间应是 14:13。
    """

    stored_value = datetime(2026, 6, 17, 6, 13, 0)
    local_value = to_local_datetime(stored_value, "Asia/Shanghai")

    assert local_value.isoformat() == "2026-06-17T14:13:00+08:00"
    assert format_local_datetime(stored_value, "Asia/Shanghai", "%m-%d %H:%M") == "06-17 14:13"


def test_source_status_api_returns_configured_local_time(db):
    """验证来源健康接口返回本地时区时间，避免前端和排障时继续看到 UTC 偏移。"""

    from app.api import source_status

    source = Source(
        name="测试来源",
        kind="steam_news",
        url="https://example.com",
        credibility=100,
        config={},
        last_checked_at=datetime(2026, 6, 17, 6, 10, 0),
        last_success_at=datetime(2026, 6, 17, 6, 13, 0),
    )
    db.add(source)
    db.commit()

    payload = source_status(db=db, settings=Settings(timezone="Asia/Shanghai", scheduler_enabled=False))

    assert payload[0]["last_checked_at"].isoformat() == "2026-06-17T14:10:00+08:00"
    assert payload[0]["last_success_at"].isoformat() == "2026-06-17T14:13:00+08:00"


def test_dashboard_renders_source_success_time_in_local_timezone(db):
    """验证首页来源健康状态使用本地时间格式化，而不是直接渲染 SQLite 的 UTC 时间。"""

    from app.main import app
    from app.database import get_db

    source = Source(
        name="测试来源",
        kind="steam_news",
        url="https://example.com",
        credibility=100,
        config={},
        enabled=True,
        last_success_at=datetime(2026, 6, 17, 6, 13, 0),
    )
    db.add(source)
    db.commit()

    def override_db():
        # 页面测试必须复用当前 fixture 的内存库，否则 TestClient 会访问应用默认数据库，断言就失去意义。
        yield db

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as client:
        response = client.get("/")
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "06-17 14:13" in response.text
