import argparse
import asyncio
import logging

from app.config import get_settings
from app.database import SessionLocal, init_db
from app.logging_config import configure_logging
from app.services.pipeline import IntelligencePipeline

logger = logging.getLogger(__name__)


async def _run(reanalyze: bool) -> None:
    settings = get_settings()
    configure_logging(settings)
    init_db()
    pipeline = IntelligencePipeline(settings)
    logger.info("开始执行事件重建命令：重新分析=%s", reanalyze)
    with SessionLocal() as db:
        stats = await pipeline.rebuild_events_from_raw(db, force_reanalyze=reanalyze)
    logger.info("事件重建命令执行完成：统计=%s", stats)
    print(stats)


def main() -> None:
    """重建事件表并可选重跑 AI 分析。"""

    parser = argparse.ArgumentParser(description="Rebuild events from existing raw_items.")
    parser.add_argument(
        "--reuse-analysis",
        action="store_true",
        help="复用已有 analyses.result_json，不重新请求 AI。",
    )
    args = parser.parse_args()
    asyncio.run(_run(reanalyze=not args.reuse_analysis))


if __name__ == "__main__":
    main()
