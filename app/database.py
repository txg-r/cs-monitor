from collections.abc import Generator

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import Settings, get_settings


class Base(DeclarativeBase):
    """所有 ORM 模型的声明基类。"""


def build_engine(settings: Settings) -> Engine:
    """创建数据库引擎，并处理 SQLite 的线程边界差异。

    FastAPI 的同步依赖可能在线程池执行，因此 SQLite 必须关闭同线程限制；PostgreSQL
    不需要该参数。这里保持分支集中，避免业务代码感知数据库类型。
    """

    connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
    engine_kwargs = {"pool_pre_ping": True, "connect_args": connect_args}
    if settings.database_url in {"sqlite:///:memory:", "sqlite://"}:
        # 内存 SQLite 默认每条连接都是独立数据库，StaticPool 才能让启动建表和请求线程共享状态。
        engine_kwargs["poolclass"] = StaticPool
    return create_engine(settings.database_url, **engine_kwargs)


settings = get_settings()
engine = build_engine(settings)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db(target_engine: Engine = engine) -> None:
    """创建 MVP 所需表，并在 PostgreSQL 上启用模糊匹配扩展。

    首版使用 create_all 降低部署复杂度。表结构进入迭代期后应切换 Alembic；这里仅创建
    缺失对象，不会删除或覆盖已有数据。
    """

    from app import models  # noqa: F401  # 导入模型后 SQLAlchemy 才能收集元数据。

    if target_engine.dialect.name == "postgresql":
        with target_engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    Base.metadata.create_all(bind=target_engine)


def get_db() -> Generator[Session, None, None]:
    """为每个请求提供独立事务，异常时回滚，避免半完成写入污染事件链。"""

    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
