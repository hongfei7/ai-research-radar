"""采集器模块"""

from radar.collectors.base import Collector
from radar.collectors.rss import RSSCollector
from radar.collectors.arxiv import ArxivCollector
from radar.collectors.hackernews import HackerNewsCollector
from radar.collectors.github_trending import GithubTrendingCollector
from radar.collectors.sec_edgar import SECEdgarCollector
from radar.collectors.web_search import WebSearchCollector

__all__ = [
    "Collector",
    "RSSCollector",
    "ArxivCollector",
    "HackerNewsCollector",
    "GithubTrendingCollector",
    "SECEdgarCollector",
    "WebSearchCollector",
]
