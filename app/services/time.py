from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def to_local_datetime(value: datetime | None, timezone_name: str) -> datetime | None:
    """把数据库时间转换为配置时区。

    SQLite 不会保留 `DateTime(timezone=True)` 的时区信息，项目里写入的时间实际是 UTC。
    因此读出无时区 datetime 时必须先按 UTC 解释，再转成本地时区；否则页面会把 UTC 误显示成本地时间。
    """

    if value is None:
        return None
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(ZoneInfo(timezone_name))


def format_local_datetime(value: datetime | None, timezone_name: str, fmt: str = "%m-%d %H:%M") -> str:
    """给模板使用的本地时间格式化函数。

    空值返回空字符串，避免模板层到处写空值判断；具体是否显示“等待首次采集”仍由模板业务逻辑决定。
    """

    local_value = to_local_datetime(value, timezone_name)
    return local_value.strftime(fmt) if local_value else ""
