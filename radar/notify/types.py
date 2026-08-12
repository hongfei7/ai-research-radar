"""结构化稿件类型 —— channel-agnostic, LLM 撰稿与渠道渲染之间的契约"""

from dataclasses import dataclass, field


# 稿件种类
KIND_MORNING = "morning"    # 晨报(每日 07:00 HKT)
KIND_DIGEST = "digest"      # 定时速递(12:30 / 18:00 HKT)
KIND_BREAKING = "breaking"  # 突发快讯(即时)

VALID_KINDS = {KIND_MORNING, KIND_DIGEST, KIND_BREAKING}


@dataclass
class DigestItem:
    """稿件中的一个事件条目"""

    title: str = ""
    tickers: list = field(default_factory=list)
    direction: str = ""           # positive | negative | neutral | mixed
    significance: int = 0         # 0-10
    summary: str = ""             # 发生了什么
    why: str = ""                 # 为什么重要
    watch: str = ""               # 接下来关注什么
    url: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "DigestItem":
        if not isinstance(d, dict):
            return cls()
        try:
            sig = int(d.get("significance", 0) or 0)
        except (TypeError, ValueError):
            sig = 0
        tickers = d.get("tickers") or []
        if not isinstance(tickers, list):
            tickers = [str(tickers)]
        return cls(
            title=str(d.get("title", "") or ""),
            tickers=[str(t) for t in tickers],
            direction=str(d.get("direction", "") or ""),
            significance=sig,
            summary=str(d.get("summary", "") or ""),
            why=str(d.get("why", "") or ""),
            watch=str(d.get("watch", "") or ""),
            url=str(d.get("url", "") or ""),
        )


@dataclass
class DigestSection:
    """稿件的一个章节: 段落型(核心观点)或条目型(要闻回顾)"""

    heading: str = ""
    paragraphs: list = field(default_factory=list)
    items: list = field(default_factory=list)  # list[DigestItem]

    @classmethod
    def from_dict(cls, d: dict) -> "DigestSection":
        if not isinstance(d, dict):
            return cls()
        paragraphs = d.get("paragraphs") or []
        if isinstance(paragraphs, str):
            paragraphs = [paragraphs]
        items = [DigestItem.from_dict(it) for it in (d.get("items") or [])]
        return cls(
            heading=str(d.get("heading", "") or ""),
            paragraphs=[str(p) for p in paragraphs],
            items=items,
        )

    def is_empty(self) -> bool:
        has_para = any(p.strip() for p in self.paragraphs)
        return not has_para and not self.items


@dataclass
class DigestPayload:
    """一份完整稿件"""

    kind: str = KIND_DIGEST
    title: str = ""
    headline: str = ""               # 一句话导语
    sections: list = field(default_factory=list)  # list[DigestSection]
    footer: str = ""
    generated_at: str = ""
    fallback: bool = False           # True = LLM 失败后的兜底模板稿

    @classmethod
    def from_dict(cls, d: dict, kind: str = KIND_DIGEST) -> "DigestPayload":
        if not isinstance(d, dict):
            raise ValueError("payload is not a dict")
        sections = [DigestSection.from_dict(s) for s in (d.get("sections") or [])]
        # 渲染层协议: 丢弃空 section(空素材硬约束)
        sections = [s for s in sections if not s.is_empty()]
        return cls(
            kind=kind,
            title=str(d.get("title", "") or ""),
            headline=str(d.get("headline", "") or ""),
            sections=sections,
            footer=str(d.get("footer", "") or ""),
            generated_at=str(d.get("generated_at", "") or ""),
            fallback=bool(d.get("fallback", False)),
        )

    def validate(self) -> None:
        """schema 校验, 不合规抛 ValueError(触发兜底)"""
        if self.kind not in VALID_KINDS:
            raise ValueError(f"invalid kind: {self.kind}")
        if not self.headline.strip() and not self.sections:
            raise ValueError("payload has neither headline nor sections")
