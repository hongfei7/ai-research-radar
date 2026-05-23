"""采集器抽象基类"""

from abc import ABC, abstractmethod
from radar.models import Item


class Collector(ABC):
    """所有采集器的抽象基类"""

    @abstractmethod
    async def fetch(self, source_id: str, params: dict) -> list[Item]:
        """
        从信源拉取数据，返回 Item 列表（仅填充采集阶段字段）。

        Args:
            source_id: 信源标识，如 "rss:openai"
            params:    config.yaml 中该信源的 params

        Returns:
            Item 列表，采集失败返回空列表
        """
        ...
