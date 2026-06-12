import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import Settings
from app.database import SessionLocal
from app.models import JobRun, utc_now
from app.services.notifier import FeishuNotifier
from app.services.pipeline import IntelligencePipeline
from app.services.reporting import DailyReportService

logger = logging.getLogger(__name__)


class SchedulerService:
    """管理进程内定时任务。

    首版系统仍是单实例调度，所以 APScheduler 就够用。
    这里保留清晰的边界，后续如果拆成独立 worker，可以只替换调度层而不动业务服务。
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.notifier = FeishuNotifier(settings)
        self.pipeline = IntelligencePipeline(settings, self.notifier)
        self.reporting = DailyReportService(settings)
        self.scheduler = AsyncIOScheduler(timezone=settings.timezone)

    def start(self) -> None:
        logger.info(
            "启动调度器：轮询间隔=%s秒，日报时间=%02d:%02d，时区=%s",
            self.settings.source_poll_tick_seconds,
            self.settings.daily_report_hour,
            self.settings.daily_report_minute,
            self.settings.timezone,
        )
        self.scheduler.add_job(
            self.poll_sources,
            "interval",
            seconds=self.settings.source_poll_tick_seconds,
            id="poll_sources",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self.retry_notifications,
            "interval",
            minutes=2,
            id="retry_notifications",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self.daily_report,
            "cron",
            hour=self.settings.daily_report_hour,
            minute=self.settings.daily_report_minute,
            id="daily_report",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            logger.info("停止调度器")
            self.scheduler.shutdown(wait=False)

    async def poll_sources(self) -> None:
        """执行一轮来源轮询，并把结果记入作业表。"""

        with SessionLocal() as db:
            run = JobRun(job_name="poll_sources", status="running")
            db.add(run)
            db.commit()
            try:
                logger.debug("开始执行来源轮询任务：任务ID=%s", run.id)
                run.details = await self.pipeline.poll_due_sources(db)
                run.status = "success"
                logger.info("来源轮询任务完成：任务ID=%s，结果=%s", run.id, run.details)
            except Exception as exc:
                # 调度任务失败要写回数据库，原因是控制台日志会滚动丢失，而任务表还能给接口和页面展示状态。
                logger.exception("来源轮询任务失败：任务ID=%s，错误=%s", run.id, exc)
                run.status = "failed"
                run.details = {"error": str(exc)[:2000]}
            finally:
                run.finished_at = utc_now()
                db.commit()

    async def retry_notifications(self) -> None:
        """重试待发送通知。"""

        with SessionLocal() as db:
            sent = await self.notifier.retry_pending(db)
            if sent:
                logger.info("通知重试完成：成功发送=%s 条", sent)

    async def daily_report(self) -> None:
        """生成并发送每日报告。"""

        with SessionLocal() as db:
            report = self.reporting.generate(db)
            sent = await self.notifier.send_report(db, report.id, report.content_markdown)
            if sent:
                report.sent_at = utc_now()
                db.commit()
                logger.info("日报发送成功：日报ID=%s，日期=%s", report.id, report.report_date)
            else:
                logger.info("日报已生成但未发送：日报ID=%s，日期=%s，已发送=%s", report.id, report.report_date, sent)
