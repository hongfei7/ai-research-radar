"""SEC EDGAR 采集器 —— 提交接口查询申报表格, 并抓取申报正文

两个曾经致命的设计问题(2026-08 修复):

1. forms 只有 8-K。8-K 是**美国本土发行人**表格, 台积电/ASML/ARM 都是外国私募
   发行人, 只报 6-K 与 20-F —— 实测三家 8-K 数量均为 0。于是这条唯一的一手通道
   对台积电结构性隐形, 83 天只产出 4 条, 而内参每期都在说"缺乏独立交叉验证"。
2. raw_summary 是一句合成占位文("X submitted Form Y on date Z"), 从不抓正文。
   即便命中, triage 也看不到任何实质内容可打分, 等于白抓。

现在短表格(8-K/6-K)会抓正文并剥掉封面样板。长表格(10-Q/10-K/20-F)正文动辄数 MB,
截断后只剩封面, 没有意义 —— 它们只登记为"该公司发了这份申报"这一事实, 具体数字
留给结构化的 XBRL 通道去取。
"""

import asyncio
import json
import logging
import re
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

from radar.collectors.base import Collector
from radar.collectors.rss import make_id, normalize_url
from radar.models import Item, utcnow_iso
from radar.credibility import get_credibility as _source_cred
from radar.htmltext import extract_text

logger = logging.getLogger(__name__)

_SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SEC_SUBMISSIONS = "https://data.sec.gov/submissions/CIK{}.json"
_SEC_ARCHIVES = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn}"
_TIMEOUT = 30
_MAX_RAW_SUMMARY = 3000      # 申报正文比 RSS 摘要更值得多留一些
_LOOKBACK_DAYS = 7           # 查最近 N 天的 filings

# 会去抓正文的表格。长表格(10-Q/10-K/20-F)正文数 MB, 截断后只剩封面样板,
# 抓了反而挤占预算; 它们的数字应当走 XBRL 结构化接口。
_TEXT_FORMS = {"8-K", "6-K"}

# 每个标的每轮最多抓几份正文, 避免申报密集时打爆 SEC 限速
_MAX_TEXT_FETCH_PER_TICKER = 3
# SEC 要求 ≤10 req/s, 这里保守取每次请求间隔
_RATE_DELAY_SEC = 0.15

# SEC 要求合法的 User-Agent
_USER_AGENT = "ai-research-radar/1.0 (personal research tool; contact@example.com)"

# 申报封面样板。6-K 每份的前 ~1500 字完全相同(表头/地址/勾选说明), 不剥掉的话
# 每条 6-K 的开头都长一个样, triage 无从分辨谁是月营收谁是董事会决议。
_BOILERPLATE_PATTERNS = (
    r"^UNITED STATES$",
    r"^SECURITIES AND EXCHANGE COMMISSION$",
    r"^Washington,?\s*D\.?C\.?\s*20549$",
    r"^FORM\s+\d+-[A-Z]$",
    r"^REPORT OF FOREIGN PRIVATE ISSUER$",
    r"^PURSUANT TO RULE 13a-16 OR 15d-16",
    r"^UNDER THE SECURITIES EXCHANGE ACT OF 1934$",
    r"^THE SECURITIES EXCHANGE ACT OF 1934$",
    r"^\(?Translation of Registrant.{0,3}s Name Into English\)?$",
    r"^\(?Address of Principal Executive Offices\)?$",
    r"^Indicate by check mark",
    r"^\d{4} Act Registration No",
    r"^\(?Commission File Number",
    r"^Form 20-F\s*☐?\s*Form 40-F",
    r"^_{3,}$",
    r"^-{3,}$",
    r"^Document$",
    r"^\d+$",
    # SEC 在线查看器包在文档最前面的那层壳: 表格代号 / 序号 / 文件名 / 描述,
    # 不剥的话每份申报都以 "EX-99.1 | a2026q2....htm | EX-99.1" 开头
    r"^EX-[\d.]+[A-Za-z]?$",
    r"^[A-Za-z0-9_\-]+\.(htm|html|txt)$",
    r"^Exhibit\s+[\d.]+[A-Za-z]?$",
    r"^Execution Version$",
)
_BOILERPLATE_RE = tuple(re.compile(p, re.I) for p in _BOILERPLATE_PATTERNS)


# SEC 自动生成的 XBRL 渲染件(R1.htm, R2.htm…)与索引页不是申报正文, 抓回来只会得到
# "IDEA: XBRL DOCUMENT / Document and Entity Information" 这类机器产物
_NON_CONTENT_RE = re.compile(
    r"^(R\d+\.htm|.*FilingSummary.*|.*index.*|\d{10}-\d\d-\d{6}.*)$", re.I
)


def _is_content_doc(name: str) -> bool:
    if not name.lower().endswith((".htm", ".html")):
        return False
    return not _NON_CONTENT_RE.match(name)


def _headline_of(body: str, max_len: int = 70) -> str:
    """取正文里第一句像样的话作标题后缀

    直接取首行会取到 "EX-99.1" 这类残留标签或公司名单行, 看不出这份申报讲什么。
    要求带空格且有一定长度, 才算一句话。
    """
    for line in body.split("\n"):
        s = line.strip()
        if len(s) >= 25 and " " in s:
            return s[:max_len]
    first = body.split("\n", 1)[0].strip()
    return first[:max_len]


def strip_filing_boilerplate(text: str) -> str:
    """剥掉 SEC 申报的封面样板, 只留下有区分度的正文"""
    if not text:
        return ""
    kept = []
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            continue
        if any(rx.match(s) for rx in _BOILERPLATE_RE):
            continue
        kept.append(s)
    return "\n".join(kept).strip()


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
        text_fetched = 0
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

            company_name = cov.get("name", ticker)

            body = ""
            if form_type in _TEXT_FORMS and text_fetched < _MAX_TEXT_FETCH_PER_TICKER:
                body = await self._fetch_filing_text(client, cik, accn_clean, doc)
                text_fetched += 1

            if body:
                # 标题带上正文里第一句像样的话: "台积电 files 6-K" 这种标题看不出是
                # 月营收还是人事变动, 而 triage 主要靠标题分辨
                title = f"{company_name} ({ticker}) {form_type}: {_headline_of(body)}"
                raw_summary = body[:_MAX_RAW_SUMMARY]
            else:
                title = f"{company_name} ({ticker}) files {form_type} — {filing_date}"
                raw_summary = (
                    f"SEC Filing: {company_name} ({ticker}) submitted Form {form_type} "
                    f"on {filing_date}. Accession: {accn}."
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

    async def _fetch_filing_text(
        self, client: httpx.AsyncClient, cik: int, accn_clean: str, primary_doc: str
    ) -> str:
        """抓申报正文并剥掉封面样板, 失败返回空串(调用方退回登记式摘要)

        实质内容有时在 exhibit 而非主文档, 所以先列目录挑最大的 .htm;
        列不到目录就退回主文档。
        """
        base = _SEC_ARCHIVES.format(cik=cik, accn=accn_clean)
        candidates: list[str] = [primary_doc] if primary_doc else []
        try:
            await asyncio.sleep(_RATE_DELAY_SEC)
            resp = await client.get(f"{base}/index.json",
                                    headers={"User-Agent": _USER_AGENT})
            resp.raise_for_status()
            files = resp.json().get("directory", {}).get("item", [])
            htm = [f for f in files if _is_content_doc(str(f.get("name", "")))]
            htm.sort(key=lambda f: int(f.get("size") or 0), reverse=True)
            for f in htm[:2]:
                if f["name"] not in candidates:
                    candidates.append(f["name"])
        except Exception as e:
            logger.debug(f"SEC index listing failed for {accn_clean}: {e}")

        # 主文档常常只是封面(实测台积电月营收的主文档 1728 字全是样板), 实质在 exhibit;
        # 但 exhibit 里最大的那份也可能是附表。逐个取回, 挑剥完样板后信息量最大的。
        best = ""
        for name in candidates[:3]:
            try:
                await asyncio.sleep(_RATE_DELAY_SEC)
                r = await client.get(f"{base}/{name}",
                                     headers={"User-Agent": _USER_AGENT})
                r.raise_for_status()
                if "IDEA: XBRL DOCUMENT" in r.text[:4000]:
                    continue        # SEC 生成的 XBRL 渲染件, 不是申报正文
                body = strip_filing_boilerplate(extract_text(r.text))
                if len(body) > len(best):
                    best = body
            except Exception as e:
                logger.debug(f"SEC document fetch failed ({name}): {e}")
        # 剥完只剩零星几个字说明整份就是封面, 退回登记式摘要
        return best if len(best) >= 120 else ""
