"""notify —— 报告生成与分发子系统

分层: assemble(数据装配) → copywriter(LLM 撰稿) → render_*(渠道版式) → transport(发送)
调度: scheduler 决定本轮该发哪些产品(每日内参/周末复盘/突发快讯)
"""

from radar.notify.types import DigestPayload, DigestSection, DigestItem  # noqa: F401
