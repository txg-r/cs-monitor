from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.models import Event, Notification, Source
from app.services.notifier import FeishuNotifier
from app.services.reporting import DailyReportService


def make_event() -> Event:
    """构造最小高价值事件，集中维护测试夹具以突出每个测试的业务意图。"""

    return Event(
        fingerprint="f1",
        title="新箱子发布",
        summary="官方发布新箱子",
        event_type="新品供给",
        direction="混合",
        impact_strength=4,
        time_horizon="1-4周",
        confidence=0.9,
        importance_score=92,
        alert_level="P0",
        is_market_relevant=True,
        verified_facts=["官方公告"],
        entities={},
        affected_assets=[{"category": "新箱子", "direction": "混合", "reason": "供给变化"}],
        market_mechanisms=["供给", "稀缺性"],
        uncertainties=[],
        first_seen_at=datetime.now(timezone.utc),
        last_seen_at=datetime.now(timezone.utc),
    )


@pytest.mark.asyncio
async def test_notification_outbox_is_idempotent_without_webhook(db):
    """验证即使飞书未配置，同一事件重复通知请求也只保留一条幂等记录。"""

    event = make_event()
    db.add(event)
    db.commit()
    notifier = FeishuNotifier(Settings(feishu_webhook_url=None, scheduler_enabled=False))
    assert await notifier.notify_event(db, event) is False
    assert await notifier.notify_event(db, event) is False
    assert db.scalar(select(func.count()).select_from(Notification)) == 1


@pytest.mark.asyncio
async def test_startup_check_records_each_start_without_webhook(db):
    """验证启动自检每次启动都会记录一条通知，用来确认每次进程启动后的推送通道状态。"""

    notifier = FeishuNotifier(Settings(feishu_webhook_url=None, scheduler_enabled=False))

    assert await notifier.send_startup_check(db) is False
    assert await notifier.send_startup_check(db) is False

    notifications = db.scalars(select(Notification).where(Notification.kind == "startup_check")).all()
    assert len(notifications) == 2
    assert {notification.status for notification in notifications} == {"disabled"}
    assert notifications[0].dedup_key != notifications[1].dedup_key


def test_daily_report_contains_events_and_source_gap(db):
    """验证日报同时展示重要事件和采集异常，防止用户把数据缺口误认为市场平静。"""

    db.add(make_event())
    db.add(Source(name="异常源", kind="rss", url="https://example.com", enabled=True, last_error="timeout", config={}))
    db.commit()
    report = DailyReportService(Settings(scheduler_enabled=False)).generate(db)
    assert "新箱子发布" in report.content_markdown
    assert "采集缺口：异常源" in report.content_markdown
