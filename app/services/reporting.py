import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import DailyReport, Event, Source, utc_now

logger = logging.getLogger(__name__)


class DailyReportService:
    """根据已结构化事件生成确定性日报，模型不可用时也能稳定产出。"""

    def __init__(self, settings: Settings):
        self.settings = settings

    def generate(self, db: Session, now: datetime | None = None) -> DailyReport:
        """生成过去 24 小时日报；同一天重复执行返回已有结果。"""

        now = now or utc_now()
        local_now = now.astimezone(ZoneInfo(self.settings.timezone))
        report_date = local_now.date().isoformat()
        logger.debug("开始生成每日报告：日报日期=%s，本地时间=%s", report_date, local_now.isoformat())
        existing = db.scalar(select(DailyReport).where(DailyReport.report_date == report_date))
        if existing:
            logger.debug("每日报告已存在，直接复用：日报日期=%s，日报ID=%s", report_date, existing.id)
            return existing

        cutoff = now - timedelta(hours=24)
        events = db.scalars(
            select(Event)
            .where(Event.first_seen_at >= cutoff, Event.alert_level.in_(["P0", "P1", "P2"]))
            .order_by(Event.importance_score.desc(), Event.first_seen_at.desc())
            .limit(8)
        ).all()
        failed_sources = db.scalars(select(Source).where(Source.enabled.is_(True), Source.last_error.is_not(None))).all()
        content = self._render(report_date, events, failed_sources)
        report = DailyReport(report_date=report_date, content_markdown=content, event_ids=[event.id for event in events])
        db.add(report)
        try:
            db.commit()
        except IntegrityError:
            # 多进程同时触发时依赖日期唯一约束收敛到同一份日报。
            db.rollback()
            return db.scalar(select(DailyReport).where(DailyReport.report_date == report_date))
        logger.info(
            "每日报告生成完成：日报ID=%s，日报日期=%s，入选事件=%s，异常来源=%s",
            report.id,
            report_date,
            len(events),
            len(failed_sources),
        )
        return report

    @staticmethod
    def _render(report_date: str, events: list[Event], failed_sources: list[Source]) -> str:
        lines = [f"CS2 饰品市场每日情报｜{report_date}", ""]
        if not events:
            lines.append("过去 24 小时未发现达到日报门槛的高价值事件。")
        else:
            for index, event in enumerate(events, 1):
                affected = "、".join(asset.get("category", "相关饰品") for asset in event.affected_assets) or "待确认"
                lines.extend(
                    [
                        f"{index}. [{event.alert_level}] {event.title}",
                        f"   方向 {event.direction}｜强度 {event.impact_strength}/5｜评分 {event.importance_score}",
                        f"   影响：{affected}｜周期：{event.time_horizon}",
                        f"   逻辑：{'、'.join(event.market_mechanisms) or '待确认'}",
                    ]
                )
        lines.extend(["", "今日观察：优先核验官方供给、交易规则和赛事贴纸相关后续信息。"])
        if failed_sources:
            names = "、".join(source.name for source in failed_sources[:5])
            lines.append(f"采集缺口：{names} 当前异常，请勿将未采集视为无事件。")
        return "\n".join(lines)
