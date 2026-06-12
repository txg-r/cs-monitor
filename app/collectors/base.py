from abc import ABC, abstractmethod

import httpx

from app.models import Source
from app.schemas import PollResult


class CollectorError(RuntimeError):
    """采集失败的统一异常，调度层据此记录来源健康状态。"""


class ConfigurationError(CollectorError):
    """来源缺少密钥或必要参数时抛出，和临时网络故障区分展示。"""


class BaseCollector(ABC):
    """采集器接口。

    采集器只负责忠实获取和初步解析内容，不在这里判断市场影响，避免来源解析规则与投资
    逻辑耦合后难以独立测试。
    """

    @abstractmethod
    async def collect(self, source: Source, client: httpx.AsyncClient) -> PollResult:
        """采集一个来源并返回标准化前的公共数据结构。"""

