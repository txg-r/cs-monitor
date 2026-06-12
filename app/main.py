from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI

from app.api import router
from app.config import get_settings
from app.database import SessionLocal, init_db
from app.logging_config import configure_logging
from app.scheduler import SchedulerService
from app.services.seeding import seed_sources


settings = get_settings()
configure_logging(settings)
logger = logging.getLogger(__name__)
scheduler_service = SchedulerService(settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """启动时初始化表和默认来源，关闭时停止调度器，避免热重载残留后台任务。"""

    logger.info("应用启动：环境=%s，数据库=%s", settings.environment, settings.database_url)
    init_db()
    with SessionLocal() as db:
        seed_sources(db, settings)
    if settings.scheduler_enabled:
        scheduler_service.start()
    try:
        yield
    finally:
        logger.info("应用关闭")
        scheduler_service.shutdown()


app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description="高价值外部信息监控、AI 市场影响判断、紧急提醒与每日总结。",
    lifespan=lifespan,
)
app.include_router(router)
