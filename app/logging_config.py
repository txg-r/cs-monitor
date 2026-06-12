import logging
import logging.config
from pathlib import Path

from app.config import Settings


_LOGGING_CONFIGURED = False


def configure_logging(settings: Settings) -> None:
    """配置应用日志。

    之前项目完全依赖 Uvicorn 默认日志，导致业务链路只有零散 INFO，排查采集、AI 和归并问题时
    基本没有上下文。这里统一接管应用日志格式和级别，并可选写入滚动文件。
    """

    global _LOGGING_CONFIGURED
    if _LOGGING_CONFIGURED:
        return

    level = settings.log_level.upper()
    handlers: dict[str, dict] = {
        "console": {
            "class": "logging.StreamHandler",
            "level": level,
            "formatter": "standard",
        }
    }
    root_handlers = ["console"]

    if settings.log_to_file:
        log_path = Path(settings.log_file_path)
        if not log_path.is_absolute():
            log_path = Path.cwd() / log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers["file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "level": level,
            "formatter": "standard",
            "filename": str(log_path),
            "maxBytes": settings.log_file_max_bytes,
            "backupCount": settings.log_file_backup_count,
            # Windows PowerShell 对无 BOM 的 UTF-8 自动识别并不稳定。
            # 这里使用 utf-8-sig，是为了让日志文件直接用 Get-Content 查看时也能稳定显示中文。
            "encoding": "utf-8-sig",
        }
        root_handlers.append("file")

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "format": "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
                    "datefmt": "%Y-%m-%d %H:%M:%S",
                }
            },
            "handlers": handlers,
            "root": {"level": level, "handlers": root_handlers},
            "loggers": {
                "uvicorn": {"level": level, "propagate": True},
                "uvicorn.error": {"level": level, "propagate": True},
                "uvicorn.access": {"level": "INFO", "propagate": True},
                "apscheduler": {"level": level, "propagate": True},
                "httpx": {"level": "WARNING", "propagate": True},
            },
        }
    )
    logging.getLogger(__name__).info(
        "日志系统已初始化：级别=%s，文件输出=%s",
        level,
        settings.log_file_path if settings.log_to_file else "关闭",
    )
    _LOGGING_CONFIGURED = True
