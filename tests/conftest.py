import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# 必须在导入数据库模块前设置，否则模块级引擎会绑定到开发库并在测试时产生本地状态文件。
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SCHEDULER_ENABLED"] = "false"

from app.database import Base  # noqa: E402


@pytest.fixture
def db():
    """为每个测试创建完全隔离的内存数据库，避免测试顺序影响事件去重结论。"""

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
