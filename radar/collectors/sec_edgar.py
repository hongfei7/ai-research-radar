"""SEC EDGAR 采集器 —— 提交接口查询 8-K 等表格"""

import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from radar.collectors.base import Collector
from radar.collectors.rss import make_id, normalize_url
from radar.models import Item, utcnow_iso
from radar.credibility import get_credibility as _source_cred

logger = logging.getLogger(__name__)

_SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{}.json"
_TIMEOUT = 30
_MAX_RAW_SUMMARY = 1500
_LOOKBACK_DAYS = 7           # 查最近 N 天的 filings

# SEC 要求合法的 User-Agent
_USER_AGENT = "ai-research-radar/1.0 (personal research tool; contact@example.com)"


def _pad_cik(cik: int) -> str:
    """CIK 补零到 10 位"""
    return str(cik).zfill(10)


def _date_to_iso(date_str: str) -> str:
    """SEC 日期格式(YYYY-MM-DD) → ISO8601"""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return date_str


def _is_recent(date_str: str, days: int = _LOOKBACK_DAYS) -> bool:
    """判断日期是否在 days 天内"""
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        return dt >= cutoff
    except Exception:
        return False


class SECEdgarCollector(Collector):
    """SEC EDGAR —— 针对 coverage 中 market=US 的标的，查询 8-K 等表格"""

    def __init__(self):
        self._ticker_map: Optional[dict[str, int]] = None  # ticker → CIK 缓存

    async def _get_ticker_map(self, client: httpx.AsyncClient) -> dict[str, int]:
        """获取 SEC company_tickers.json，缓存结果"""
        if self._ticker_map is not None:
            return self._ticker_map

        try:
            resp = await client.get(
                _SEC_TICKERS_URL,
                headers={"User-Agent": _USER_AGENT},
            )
            resp.raise_for_status()
            data = resp.json()

            # 返回格式: {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
            ticker_map: dict[str, int] = {}
            for v in data.values():
                ticker = v.get("ticker", "").upper()
                cik = v.get("cik_str", 0)
                if ticker and cik:
                    ticker_map[ticker] = cik
            self._ticker_map = ticker_map
            logger.info(f"SEC EDGAR: loaded {len(ticker_map)} ticker→CIK mappings")
            return ticker_map
        except Exception as e:
            logger.error(f"SEC EDGAR: failed to load ticker map: {e}")
            return {}

    async def fetch(self, source_id: str, params: dict) -> list[Item]:
        """
        cfg 中的 coverage 和 forms 需要通过外部传入。
        这里使用一个简化的方法：通过 cfg 的 coverage 列表查找 US 标的。
        """
        forms = params.get("forms", ["8-K"])
        fetched_at = utcnow_iso()

        items: list[Item] = []
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            ticker_map = await self._get_ticker_map(client)
            if not ticker_map:
                return []

            # 从 config 中获取 coverage（在 fetch 时通过 collector 实例变量传入）
            us_tickers = self._get_us_coverage()

            for cov in us_tickers:
                ticker = cov["ticker"].upper()
                cik = ticker_map.get(ticker)
                if not cik:
                    logger.debug(f"[{source_id}] No CIK found for {ticker}")
                    continue

                try:
                    batch = await self._fetch_filings(client, cik, ticker, cov, forms, source_id, fetched_at)
                    items.extend(batch)
                except Exception as e:
                    logger.error(f"[{source_id}] Failed for {ticker} (CIK {cik}): {e}")
                    continue

        logger.info(f"[{source_id}] Fetched {len(items)} SEC filings")
        return items

    def _get_us_coverage(self) -> list[dict]:
        """从 config 获取 US 标的（通过类变量注入）"""
        # 这个在 main.py 采集时通过 collector 的 cfg 属性获取
        if hasattr(self, "_coverage"):
            return [c for c in self._coverage if c.get("market") == "US" and c.get("ticker")]
        return []

    def set_coverage(self, coverage: list[dict]) -> None:
        """注入覆盖标的列表"""
        self._coverage = coverage

    async def _fetch_filings(
        self,
        client: httpx.AsyncClient,
        cik: int,
        ticker: str,
        cov: dict,
        forms: list[str],
        source_id: str,
        fetched_at: str,
    ) -> list[Item]:
        """查询单个 CIK 的 submissions"""
        cik_padded = _pad_cik(cik)
        url = _SEC_SUBMISSIONS.format(cik_padded)

        resp = await client.get(
            url,
            headers={"User-Agent": _USER_AGENT},
        )
        resp.raise_for_status()
        data = resp.json()

        filings = data.get("filings", {}).get("recent", {})
        if not filings:
            return []

        form_list = filings.get("form", [])
        date_list = filings.get("filingDate", [])
        accn_list = filings.get("accessionNumber", [])
        primary_docs = filings.get("primaryDocument", [])

        items: list[Item] = []
        for i in range(len(form_list)):
            form_type = form_list[i] if i < len(form_list) else ""
            filing_date = date_list[i] if i < len(date_list) else ""
            accn = accn_list[i] if i < len(accn_list) else ""
            doc = primary_docs[i] if i < len(primary_docs) else ""

            # 过滤：只看指定表格 + 近期
            if form_type not in forms:
                continue
            if not _is_recent(filing_date, _LOOKBACK_DAYS):
                continue

            # 构造 SEC 文档链接
            accn_clean = accn.replace("-", "")
            doc_url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accn_clean}/{doc}"
            filing_url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={form_type}"

            company_name = cov.get("name", ticker)
            title = f"{company_name} ({ticker}) files {form_type} — {filing_date}"
            raw_summary = (
                f"SEC Filing: {company_name} ({ticker}) submitted Form {form_type} "
                f"on {filing_date}. Accession: {accn}. "
                f"View filing at {doc_url}"
            )[:_MAX_RAW_SUMMARY]

            item = Item(
                id=make_id(accn),
                title=title,
                url=normalize_url(doc_url),
                source=f"{source_id}:{form_type}",
                source_type="market",
                published_at=_date_to_iso(filing_date),
                fetched_at=fetched_at,
                raw_summary=raw_summary,
                credibility=_source_cred(source_id),
                image_url="",
            )
            items.append(item)

        return items
