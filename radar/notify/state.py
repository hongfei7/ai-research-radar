"""notify_state.json 读写 —— 推送去重键与快讯指纹

与 Situation 解耦(审计 H2): 推送状态独立存放, 灰度期 Situation 旧字段双写保留。
at-least-once 语义: state 丢失可能重发, 由强去重键(日期键/内容指纹)降低概率。
"""

import json
import logging
from pathlib import Path

from radar.models import utcnow_iso

logger = logging.getLogger(__name__)

_STATE_PATH = Path(__file__).resolve().parent.parent.parent / "state" / "notify_state.json"

_DEFAULT = {
    "morning_last_date": "",       # 每日内参当日去重键 "2026-08-12"
    "weekly_last_slot": "",        # 周末复盘周去重键 "2026-W32"
    "breaking_fingerprints": [],   # 快报指纹 [{tickers, features, sent_at}]
    # 上一期内参的宏观框架与判断, 供下一期做框架增量修订与证伪回溯。
    # 没有这份状态, 每期报告都是孤立的, 写下的证伪条件永远无人核对。
    "last_report": None,           # {date, macro: {...}, calls: [{claim, falsifier, verify}]}
    # 当日降级补发进度 {date, attempts, issue_number, issue_url}。降级稿不占用
    # morning_last_date, 靠这个键记住"今天已经推过降级稿、还欠一份完整稿"。
    "morning_fallback": None,
    # 盲区台账 [{key, text, first_seen, last_seen, times_seen}]。
    # 过去盲区写完即焚(MacroFrame.to_state 不带它), 于是同一个洞可以年复一年地
    # 被复述而无人去堵。台账单独存放, 不塞进 macro —— 那里要保持框架基线干净。
    "blindspot_ledger": [],
}


def load_notify_state() -> dict:
    """加载推送状态, 不存在或损坏时返回默认值"""
    if not _STATE_PATH.exists():
        return dict(_DEFAULT)
    try:
        with open(_STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        state = dict(_DEFAULT)
        state.update({k: v for k, v in data.items() if k in state})
        if not isinstance(state["breaking_fingerprints"], list):
            state["breaking_fingerprints"] = []
        if not isinstance(state["last_report"], dict):
            state["last_report"] = None
        if not isinstance(state["morning_fallback"], dict):
            state["morning_fallback"] = None
        if not isinstance(state["blindspot_ledger"], list):
            state["blindspot_ledger"] = []
        return state
    except Exception as e:
        logger.warning(f"Failed to load notify_state: {e}")
        return dict(_DEFAULT)


def save_notify_state(state: dict) -> Path:
    """保存推送状态"""
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    logger.info(f"Saved notify_state to {_STATE_PATH}")
    return _STATE_PATH


# 盲区连续复现多少期后升级为"通道问题"。取 5 —— 连着五期还在说同一个洞,
# 那已经不是"今天恰好不知道", 是这套管道拿不到, 该去加信源或修采集器了。
BLINDSPOT_ESCALATE_TIMES = 5

_STOPWORDS = "的了与和及对在是有为将把被这那我们本期素材完全未涉及一二三"


def blindspot_key(text: str, coverage_names: list[str] | None = None) -> str:
    """给盲区取一个跨期稳定的键

    不能拿原文比对 —— LLM 每期措辞都会漂("台积电缺乏一手披露"下一期可能写成
    "对台积电的一手披露仍然缺失")。

    优先用**文中提到的覆盖标的**做键: 这类盲区几乎总是围绕具体标的, 标的集合比
    句式稳定得多。文中一个标的都没提到时才退回字符特征。
    (试过纯字符集 Jaccard, 两头不讨好: 换句式认不出, 而"编号1/编号2"这种只差一个
     数字的又会被合并成一条。)
    """
    text = text or ""
    names = sorted({n for n in (coverage_names or []) if n and n in text})
    if names:
        return "|".join(names)
    chars = {c for c in text if "一" <= c <= "鿿" and c not in _STOPWORDS}
    return "".join(sorted(chars))[:24]


def record_blindspot(state: dict, text: str, today: str,
                     coverage_names: list[str] | None = None) -> dict:
    """把本期盲区记进台账, 返回该条台账项

    同一个盲区反复出现时 times_seen 递增; 达到 BLINDSPOT_ESCALATE_TIMES 即视为
    结构性缺口 —— 那是给人看的信号: 别再每期复述, 去把通道补上。
    """
    text = (text or "").strip()
    if not text:
        return {}
    ledger = state.setdefault("blindspot_ledger", [])
    key = blindspot_key(text, coverage_names)

    for entry in ledger:
        if entry.get("key") == key:
            entry["times_seen"] = entry.get("times_seen", 1) + 1
            entry["last_seen"] = today
            entry["text"] = text          # 留最新措辞
            return entry

    entry = {"key": key, "text": text, "first_seen": today,
             "last_seen": today, "times_seen": 1}
    ledger.append(entry)
    return entry


def prune_blindspots(state: dict, keep: int = 12) -> dict:
    """台账只留最近出现过的若干条, 防止无限增长"""
    ledger = state.get("blindspot_ledger") or []
    ledger.sort(key=lambda e: (e.get("last_seen", ""), e.get("times_seen", 0)),
                reverse=True)
    state["blindspot_ledger"] = ledger[:keep]
    return state


def escalated_blindspots(state: dict) -> list[dict]:
    """连续复现到该去修通道的那些"""
    return [e for e in (state.get("blindspot_ledger") or [])
            if e.get("times_seen", 0) >= BLINDSPOT_ESCALATE_TIMES]


def prune_fingerprints(state: dict, cooldown_min: int, now_iso: str | None = None) -> dict:
    """清理冷却期外的快讯指纹(只保留最近 24h, 防止无限增长)"""
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    keep_horizon = now - timedelta(hours=24)
    kept = []
    for fp in state.get("breaking_fingerprints", []):
        try:
            sent = datetime.fromisoformat(str(fp.get("sent_at", "")).replace("Z", "+00:00"))
            if sent >= keep_horizon:
                kept.append(fp)
        except Exception:
            continue
    state["breaking_fingerprints"] = kept
    return state
