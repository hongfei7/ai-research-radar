"""结构化稿件类型 —— channel-agnostic, LLM 撰稿与渠道渲染之间的契约

每日内参走「判断链」形态(calls), 四层结构:
  宏观 macro → 速览/判断 calls(事实→机理→推论→证伪) → 中观 tension → 跨期 reviews
快讯/复盘仍走通用的 sections 形态。

证据一律用 ref(E1…En)引用, 稿件里不出现 URL —— 渲染层负责把 ref 还原成链接。
"""

from dataclasses import dataclass, field
from typing import Optional


# 稿件种类
KIND_MORNING = "morning"    # 每日内参(每日 07:00 HKT, 旗舰研报)
KIND_WEEKLY = "weekly"      # 周末复盘(周日晚)
KIND_BREAKING = "breaking"  # 首席快报(即时)

VALID_KINDS = {KIND_MORNING, KIND_WEEKLY, KIND_BREAKING}

# 宏观框架相对上期的变化类型
SHIFT_KINDS = {"hold", "adjust", "pivot"}
SHIFT_LABEL = {"hold": "维持", "adjust": "微调", "pivot": "转向"}

# 上期判断的回溯结论
REVIEW_STATUSES = ("已验证", "进展中", "接近证伪", "已证伪")


def _as_str(v) -> str:
    return str(v).strip() if v is not None else ""


def _as_str_list(v) -> list:
    if not v:
        return []
    if isinstance(v, str):
        return [v.strip()] if v.strip() else []
    if not isinstance(v, list):
        return [str(v).strip()]
    return [str(x).strip() for x in v if str(x).strip()]


@dataclass
class DigestItem:
    """一个事件条目 —— 快报正文与兜底稿共用

    summary / why / watch 正是快报要的三句(发生了什么 / 为什么重要 / 盯什么)。
    url 不由 LLM 提供, 引用一律走 evidence_ref, 渲染层查表还原。
    """

    title: str = ""
    tickers: list = field(default_factory=list)
    direction: str = ""           # positive | negative | neutral | mixed
    significance: int = 0         # 0-10
    summary: str = ""             # 发生了什么
    why: str = ""                 # 为什么重要
    watch: str = ""               # 接下来关注什么
    url: str = ""
    evidence_ref: str = ""        # 素材里的证据编号(E1…)

    @classmethod
    def from_dict(cls, d: dict) -> "DigestItem":
        if not isinstance(d, dict):
            return cls()
        try:
            sig = int(d.get("significance", 0) or 0)
        except (TypeError, ValueError):
            sig = 0
        return cls(
            title=_as_str(d.get("title")),
            tickers=_as_str_list(d.get("tickers")),
            direction=_as_str(d.get("direction")),
            significance=sig,
            summary=_as_str(d.get("summary")),
            why=_as_str(d.get("why")),
            watch=_as_str(d.get("watch")),
            url=_as_str(d.get("url")),
            evidence_ref=_as_str(d.get("evidence_ref")),
        )

    def missing_parts(self) -> list[str]:
        """快报三段里缺的部分, 空列表表示完整"""
        required = {"summary": self.summary, "why": self.why, "watch": self.watch}
        return [k for k, v in required.items() if not v.strip()]


@dataclass
class DigestSection:
    """稿件的一个章节: 段落型或条目型"""

    heading: str = ""
    paragraphs: list = field(default_factory=list)
    items: list = field(default_factory=list)  # list[DigestItem]

    @classmethod
    def from_dict(cls, d: dict) -> "DigestSection":
        if not isinstance(d, dict):
            return cls()
        items = [DigestItem.from_dict(it) for it in (d.get("items") or [])]
        return cls(
            heading=_as_str(d.get("heading")),
            paragraphs=_as_str_list(d.get("paragraphs")),
            items=items,
        )

    def is_empty(self) -> bool:
        has_para = any(p.strip() for p in self.paragraphs)
        return not has_para and not self.items


@dataclass
class MacroTrajectory:
    """主线轨迹 —— 产业主约束的迁移路径(过去 → 当下 → 未来)

    单说"当前主约束"是一张静态快照, 读者看不出这个约束是刚形成还是快过去了。
    trigger 让"未来"这一段可被观测检验, 而不是无法证伪的预言。
    """

    past: str = ""      # 上一阶段的主约束
    now: str = ""       # 当下的主约束
    next: str = ""      # 下一阶段可能的主约束
    why_now: str = ""   # 当下这一段为什么是它 —— 没有它, 轨迹只是三个标签
    trigger: str = ""   # 什么可观测信号出现即视为进入下一阶段

    @classmethod
    def from_dict(cls, d: dict) -> "MacroTrajectory":
        if not isinstance(d, dict):
            return cls()
        return cls(
            past=_as_str(d.get("past")),
            now=_as_str(d.get("now")),
            next=_as_str(d.get("next")),
            why_now=_as_str(d.get("why_now")),
            trigger=_as_str(d.get("trigger")),
        )

    def is_empty(self) -> bool:
        return not self.now.strip()

    def to_state(self) -> dict:
        return {"past": self.past, "now": self.now, "next": self.next,
                "why_now": self.why_now, "trigger": self.trigger}


@dataclass
class MacroFrame:
    """宏观框架 —— 跨期演化, 不是每期重新发明"""

    cycle: str = ""        # 周期位置(必须带量化锚点)
    trajectory: MacroTrajectory = field(default_factory=MacroTrajectory)
    # 本期证据对框架的检验: 强化了哪一部分、哪里出现反向信号。
    # 没有它, 格局与判断链就是两个并列的独立段落, 读者看不出宏观怎么被验证
    check: str = ""
    # 盲区: 我们不知道什么。宏观层最该承载的就是已知的未知,
    # 素材里 cross_analysis 的「信息盲点」此前完全没被取用
    blindspot: str = ""
    shift: str = ""        # 相对上期的变化说明
    shift_kind: str = ""   # hold | adjust | pivot

    @classmethod
    def from_dict(cls, d: dict) -> "MacroFrame":
        if not isinstance(d, dict):
            return cls()
        kind = _as_str(d.get("shift_kind")).lower()
        traj = MacroTrajectory.from_dict(d.get("trajectory") or {})
        # 兼容只给了 constraint 的旧写法(以及 LLM 偶尔漏掉 trajectory 的情况)
        if traj.is_empty() and d.get("constraint"):
            traj.now = _as_str(d.get("constraint"))
        return cls(
            cycle=_as_str(d.get("cycle")),
            trajectory=traj,
            check=_as_str(d.get("check")),
            blindspot=_as_str(d.get("blindspot")),
            shift=_as_str(d.get("shift")),
            shift_kind=kind if kind in SHIFT_KINDS else "hold",
        )

    def is_empty(self) -> bool:
        return not self.cycle.strip() and self.trajectory.is_empty()

    def to_state(self) -> dict:
        """写入 notify_state 供下期做增量修订

        只留框架本身。check 与 blindspot 是当期产物, 写进跨期状态会把
        "昨天看到了什么"混进"我们的框架是什么", 污染下一期的修订基线。
        """
        return {
            "cycle": self.cycle,
            "trajectory": self.trajectory.to_state(),
            "shift": self.shift,
            "shift_kind": self.shift_kind,
        }


@dataclass
class CallReview:
    """上期某条判断的回溯结论"""

    claim: str = ""
    falsifier: str = ""
    status: str = ""       # 已验证 | 进展中 | 接近证伪 | 已证伪
    basis: str = ""
    evidence_refs: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "CallReview":
        if not isinstance(d, dict):
            return cls()
        status = _as_str(d.get("status"))
        return cls(
            claim=_as_str(d.get("claim")),
            falsifier=_as_str(d.get("falsifier")),
            status=status if status in REVIEW_STATUSES else "进展中",
            basis=_as_str(d.get("basis")),
            evidence_refs=_as_str_list(d.get("evidence_refs")),
        )

    def is_empty(self) -> bool:
        return not self.claim.strip()


@dataclass
class DigestCall:
    """一条判断 —— 事实 → 机理 → 推论 → 证伪条件"""

    n: int = 0
    claim: str = ""          # 判断句标题
    direction: str = ""      # 方向, 如 "台积电 / 长电 ↑"
    verify: str = ""         # 速览表用的短验证点
    fact: str = ""
    mechanism: str = ""
    inference: str = ""
    falsifier: str = ""
    counterpoint: str = ""
    evidence_refs: list = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict, n: int = 0) -> "DigestCall":
        if not isinstance(d, dict):
            return cls(n=n)
        try:
            idx = int(d.get("n", n) or n)
        except (TypeError, ValueError):
            idx = n
        return cls(
            n=idx,
            claim=_as_str(d.get("claim")),
            direction=_as_str(d.get("direction")),
            verify=_as_str(d.get("verify")),
            fact=_as_str(d.get("fact")),
            mechanism=_as_str(d.get("mechanism")),
            inference=_as_str(d.get("inference")),
            falsifier=_as_str(d.get("falsifier")),
            counterpoint=_as_str(d.get("counterpoint")),
            evidence_refs=_as_str_list(d.get("evidence_refs")),
        )

    def missing_parts(self) -> list[str]:
        """四段式里缺的部分, 空列表表示完整"""
        required = {
            "claim": self.claim, "fact": self.fact, "mechanism": self.mechanism,
            "inference": self.inference, "falsifier": self.falsifier,
        }
        return [k for k, v in required.items() if not v.strip()]

    def to_state(self) -> dict:
        """写入 notify_state 供下期回溯, 只留检验所需字段"""
        return {"claim": self.claim, "falsifier": self.falsifier, "verify": self.verify}


@dataclass
class DigestPayload:
    """一份完整稿件"""

    kind: str = KIND_MORNING
    title: str = ""
    headline: str = ""               # 一句话导语
    macro: MacroFrame = field(default_factory=MacroFrame)
    calls: list = field(default_factory=list)     # list[DigestCall], 每日内参主体
    tension: str = ""                # 判断之间的张力与联动
    reviews: list = field(default_factory=list)   # list[CallReview]
    watchlist: list = field(default_factory=list) # [{text, ref}] 未进入判断的观察
    alert: Optional[DigestItem] = None            # 快报正文(单事件三段)
    sections: list = field(default_factory=list)  # list[DigestSection], 复盘
    footer: str = ""
    generated_at: str = ""
    fallback: bool = False           # True = LLM 失败后的兜底模板稿
    # True = 当日已推过降级稿, 这一份是补发的修订版。渲染层据此标注,
    # 让读者知道它替代的是早上那份, 而不是当作第二份内参
    revision: bool = False

    @classmethod
    def from_dict(cls, d: dict, kind: str = KIND_MORNING) -> "DigestPayload":
        if not isinstance(d, dict):
            raise ValueError("payload is not a dict")
        sections = [DigestSection.from_dict(s) for s in (d.get("sections") or [])]
        # 渲染层协议: 丢弃空 section(空素材硬约束)
        sections = [s for s in sections if not s.is_empty()]

        calls = []
        for i, c in enumerate(d.get("calls") or [], 1):
            call = DigestCall.from_dict(c, n=i)
            call.n = i  # 序号由位置决定, 不信任 LLM 自己编的号
            calls.append(call)

        reviews = [CallReview.from_dict(r) for r in (d.get("reviews") or [])]
        reviews = [r for r in reviews if not r.is_empty()]

        watchlist = []
        for w in (d.get("watchlist") or []):
            if isinstance(w, dict):
                text = _as_str(w.get("text"))
                ref = _as_str(w.get("ref"))
            else:
                text, ref = _as_str(w), ""
            if text:
                watchlist.append({"text": text, "ref": ref})

        alert_raw = d.get("alert")
        alert = DigestItem.from_dict(alert_raw) if isinstance(alert_raw, dict) else None

        return cls(
            kind=kind,
            title=_as_str(d.get("title")),
            headline=_as_str(d.get("headline")),
            macro=MacroFrame.from_dict(d.get("macro") or {}),
            calls=calls,
            tension=_as_str(d.get("tension")),
            reviews=reviews,
            watchlist=watchlist,
            alert=alert,
            sections=sections,
            footer=_as_str(d.get("footer")),
            generated_at=_as_str(d.get("generated_at")),
            fallback=bool(d.get("fallback", False)),
        )

    def prune_refs(self, valid_refs: set) -> int:
        """剔除素材里不存在的证据 ref, 返回剔除个数

        LLM 偶尔会引用不存在的编号; 留着会渲染出空证据行。
        """
        dropped = 0
        for call in self.calls:
            kept = [r for r in call.evidence_refs if r in valid_refs]
            dropped += len(call.evidence_refs) - len(kept)
            call.evidence_refs = kept
        for review in self.reviews:
            kept = [r for r in review.evidence_refs if r in valid_refs]
            dropped += len(review.evidence_refs) - len(kept)
            review.evidence_refs = kept
        kept_watch = []
        for w in self.watchlist:
            if w.get("ref") and w["ref"] not in valid_refs:
                dropped += 1
                w = {**w, "ref": ""}
            kept_watch.append(w)
        self.watchlist = kept_watch
        if self.alert and self.alert.evidence_ref and self.alert.evidence_ref not in valid_refs:
            dropped += 1
            self.alert.evidence_ref = ""
        return dropped

    def validate(self, expect_calls: bool = False, expect_reviews: int = 0,
                 expect_alert: bool = False) -> None:
        """schema 校验, 不合规抛 ValueError(触发兜底)

        Args:
            expect_calls:   是否要求判断链形态(每日内参)
            expect_reviews: 上期判断条数, >0 时要求本期给出等量回溯
            expect_alert:   是否要求快报三段形态
        """
        if self.kind not in VALID_KINDS:
            raise ValueError(f"invalid kind: {self.kind}")

        if expect_alert:
            if self.alert is None:
                raise ValueError("breaking payload has no alert")
            missing = self.alert.missing_parts()
            if missing:
                raise ValueError(f"alert missing: {', '.join(missing)}")
            return

        if not expect_calls:
            has_body = bool(self.sections) or self.alert is not None
            if not self.headline.strip() and not has_body:
                raise ValueError("payload has neither headline, sections nor alert")
            return

        if not self.calls:
            raise ValueError("morning payload has no calls")
        if len(self.calls) > 5:
            raise ValueError(f"too many calls: {len(self.calls)}")
        for call in self.calls:
            missing = call.missing_parts()
            if missing:
                raise ValueError(f"call {call.n} missing: {', '.join(missing)}")
            if not call.evidence_refs:
                raise ValueError(f"call {call.n} has no valid evidence refs")
        if self.macro.is_empty():
            raise ValueError("macro frame is empty")
        if not self.macro.trajectory.now.strip():
            raise ValueError("macro trajectory has no current constraint")
        # 只要有判断就一定写得出本期检验; 盲区允许为空(素材真没盲点时不逼它编)
        if not self.macro.check.strip():
            raise ValueError("macro frame has no check against this period's evidence")
        if expect_reviews and len(self.reviews) < expect_reviews:
            raise ValueError(
                f"expected {expect_reviews} call reviews, got {len(self.reviews)}"
            )
