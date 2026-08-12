"""notify —— 推送子系统(重构版, 替代 radar/publish.py 的格式化/推送职责)

分层: assemble(数据装配) → copywriter(LLM 撰稿) → render_*(渠道版式) → transport(发送)
调度: scheduler 决定本轮该发哪些产品(晨报/定时速递/突发快讯)
"""

from radar.notify.types import DigestPayload, DigestSection, DigestItem  # noqa: F401
