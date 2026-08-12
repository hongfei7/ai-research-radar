"""notify 编排器 —— run(): 调度 → 装配 → 撰稿 → 渲染 → 发送 → 落状态

与管道正交(审计 S1/F3): 自state装配素材, 自持 MinimaxClient 生命周期,
main.py 三条路径汇合后统一调用, 不依赖本轮是否有新条目。
"""

import logging
from pathlib import Path
from typing import Optional

from radar.minimax_client import MinimaxClient
from radar.models import Event, Item, Situation, utcnow_iso, today_str
from radar.notify import assemble, copywriter, render_wecom, render_telegram, transport
from radar.notify.readme_index import update_readme_index
from radar.notify.render_issue import render_issue_body, render_report_file
from radar.notify.scheduler import decide, content_fingerprint, PushTask
from radar.notify.state import load_notify_state, save_notify_state, prune_fingerprints
from radar.notify.types import KIND_MORNING, KIND_WEEKLY, KIND_BREAKING

logger = logging.getLogger(__name__)

_REPORTS_DIR = Path(__file__).resolve().parent.parent.parent / "reports"
_BREAKING_LABEL = "快报"


def _themes_map(cfg: dict) -> dict:
    return {t["key"]: t["name"] for t in cfg.get("themes", [])}


def _brand(cfg: dict) -> dict:
    from radar.notify.brand import brand_of
    return brand_of(cfg)


def _apply_message_budget(messages: list[str], budget: int,
                          payload_title: str, link: str) -> list[str]:
    """每轮消息预算(审计 S4): 超限合并为一条汇总引流消息"""
    if len(messages) <= budget:
        return messages
    if budget <= 1:
        kept = messages[:1]
    else:
        kept = messages[:budget - 1]
    overflow = f"**{payload_title}(余下章节略)**"
    if link:
        overflow += f"\n[完整内容]({link})"
    kept.append(overflow)
    logger.warning(f"Message budget: {len(messages)} → {len(kept)} (budget {budget})")
    return kept


async def _execute_task(
    task: PushTask,
    cfg: dict,
    client: MinimaxClient,
    situation: Optional[Situation],
    site_url: str,
    state: dict,
    dry_run: bool,
    budget: int,
    now_hkt_str: str,
) -> tuple[int, bool]:
    """执行单个推送任务, 返回 (消耗的消息预算, 是否任一渠道成功)"""

    # —— 装配素材 ——
    if task.kind == KIND_MORNING:
        material = assemble.assemble_material(hours=24, situation=situation,
                                              themes_map=_themes_map(cfg),
                                              last_report=state.get("last_report"))
    elif task.kind == KIND_WEEKLY:
        material = assemble.assemble_material(hours=168, situation=situation,
                                              themes_map=_themes_map(cfg))
        # 空素材协议: 复盘零事件静默
        if not material.get("events"):
            logger.info("Weekly skipped: no events in window (silent)")
            return 0, True  # 视为成功, 写入去重键避免反复评估
    else:  # breaking —— 单事件分别推送
        return await _execute_breaking(task, cfg, client, site_url, state,
                                       dry_run, budget, now_hkt_str)

    # —— LLM 撰稿 ——
    payload = await copywriter.write_digest(task.kind, material, cfg, client,
                                            current_time_hkt=now_hkt_str)

    # —— 内参/复盘: 幂等创建 Issue + 仓库归档(审计 H2) ——
    body = render_issue_body(payload, site_url, material=material, brand=_brand(cfg))
    issue_url = ""
    if task.kind in (KIND_MORNING, KIND_WEEKLY) and not dry_run:
        if task.kind == KIND_MORNING:
            issue_label = "晨报"
            issue_title = f"AI 首席内参 · {today_str()}"
            report_slug = today_str()
        else:
            issue_label = "周报"
            issue_title = f"Sterling 周末复盘 · {task.slot_key}"
            report_slug = f"weekly-{task.slot_key}"
        issue_url = await transport.find_today_issue(issue_title, label=issue_label) or ""
        if not issue_url:
            issue_url = await transport.create_issue(issue_title, body, [issue_label]) or ""
        if issue_url:
            logger.info(f"Issue archived: {issue_url}")
        # 仓库内 Markdown 归档: 可检索、可 diff、可离线阅读, 不依赖 GitHub API 成功
        _write_report_file(payload, body, issue_url, report_slug)
        if task.kind == KIND_MORNING:
            try:
                update_readme_index(issue_url)
            except Exception as e:
                logger.warning(f"README index update failed: {e}")

    # —— 渲染 ——
    brand = _brand(cfg)
    wecom_msgs = render_wecom.render(payload, site_url, issue_url, brand=brand)
    wecom_msgs = _apply_message_budget(wecom_msgs, budget, payload.title,
                                       issue_url or site_url)
    tg_msg = render_telegram.render(payload, site_url, issue_url, brand=brand,
                                    material=material)

    # —— 发送 / dry-run ——
    if dry_run:
        _print_dry_run(task, payload, wecom_msgs, tg_msg, body)
        return len(wecom_msgs), True

    success = False
    if cfg.get("channels", {}).get("wecom", {}).get("enabled", False):
        sent = await transport.send_wecom_messages(wecom_msgs)
        success = success or sent > 0
    if cfg.get("channels", {}).get("telegram", {}).get("enabled", False):
        success = await transport.send_telegram_html(tg_msg) or success
    success = success or bool(issue_url)

    # 留给下一期做框架修订与证伪回溯的锚点; 降级稿不写, 否则会用空框架覆盖好框架
    if success and task.kind == KIND_MORNING and not payload.fallback:
        state["last_report"] = {
            "date": today_str(),
            "macro": payload.macro.to_state(),
            "calls": [c.to_state() for c in payload.calls],
        }

    return len(wecom_msgs), success


async def _execute_breaking(task, cfg, client, site_url, state,
                            dry_run, budget, now_hkt_str) -> tuple[int, bool]:
    """突发快讯: 一事件一条"""
    used = 0
    any_success = False
    for ev in task.events:
        if used >= budget:
            logger.info(f"Breaking budget exhausted, {ev.title[:30]} 并入下轮评估")
            break
        items = task.items_by_event.get(ev.event_id, [])
        material = assemble.assemble_breaking_material(ev, items)
        payload = await copywriter.write_digest(
            KIND_BREAKING, material, cfg, client, current_time_hkt=now_hkt_str
        )
        body = render_issue_body(payload, site_url, material=material, brand=_brand(cfg))

        # 先归档再推送: 推送里要带上汇总 Issue 的链接
        issue_url = "" if dry_run else await _archive_breaking(payload, body, now_hkt_str)

        wecom_msgs = render_wecom.render(payload, site_url, issue_url,
                                         brand=_brand(cfg), material=material)
        tg_msg = render_telegram.render(payload, site_url, issue_url,
                                        brand=_brand(cfg), material=material)

        if dry_run:
            _print_dry_run(task, payload, wecom_msgs, tg_msg, body)
            used += len(wecom_msgs)
            any_success = True
            continue

        success = False
        if cfg.get("channels", {}).get("wecom", {}).get("enabled", False):
            success = (await transport.send_wecom_messages(wecom_msgs)) > 0
        if cfg.get("channels", {}).get("telegram", {}).get("enabled", False):
            success = await transport.send_telegram_html(tg_msg) or success
        success = success or bool(issue_url)

        if success:
            # 记录内容签名指纹(防重)
            fp = content_fingerprint(ev, items)
            fp["sent_at"] = utcnow_iso()
            state.setdefault("breaking_fingerprints", []).append(fp)
            any_success = True
        used += len(wecom_msgs)
    return used, any_success


async def _archive_breaking(payload, body: str, now_hkt_str: str) -> str:
    """快报归档: 当日一个汇总 Issue, 首条建 Issue, 后续追加为评论

    一天可能推十几条快报, 一条一个 Issue 会淹没仓库; 汇总成一个则既能溯源,
    也能被日报的「上期回溯」当作当日事实底稿翻查。
    """
    title = f"AI 快报 · {today_str()}"
    entry = f"### {now_hkt_str}\n\n{body}"
    issue_url = ""
    try:
        found = await transport.find_issue(title, label=_BREAKING_LABEL)
        if found:
            issue_url = found.get("html_url") or ""
            await transport.add_issue_comment(found.get("number"), entry)
        else:
            issue_url = await transport.create_issue(
                title, entry, [_BREAKING_LABEL]) or ""
    except Exception as e:
        logger.warning(f"Breaking archive failed: {e}")

    _append_report_file(f"breaking-{today_str()}", entry)
    return issue_url


def _append_report_file(slug: str, entry: str) -> None:
    """追加写入 reports/<slug>.md; 文件不存在时带上 frontmatter"""
    try:
        _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        path = _REPORTS_DIR / f"{slug}.md"
        if not path.exists():
            header = (f"---\ndate: {today_str()}\nkind: breaking\n---\n\n"
                      f"# AI 快报 · {today_str()}\n\n")
            path.write_text(header, encoding="utf-8")
        with open(path, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n---\n\n")
        logger.info(f"Breaking archived: {path}")
    except OSError as e:
        logger.warning(f"Breaking report append failed: {e}")


def _write_report_file(payload, body: str, issue_url: str, slug: str) -> None:
    """把内参正文归档到 reports/<slug>.md"""
    try:
        _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        content = render_report_file(payload, body, issue_url, date_str=today_str())
        path = _REPORTS_DIR / f"{slug}.md"
        path.write_text(content, encoding="utf-8")
        logger.info(f"Report archived: {path}")
    except OSError as e:
        logger.warning(f"Report archive failed: {e}")


def _print_dry_run(task, payload, wecom_msgs, tg_msg, issue_body: str = "") -> None:
    sep = "=" * 60
    print(f"\n{sep}\n[DRY-RUN] {task.kind} ({task.reason}) fallback={payload.fallback}\n{sep}")
    if issue_body:
        print(f"\n--- Issue / reports 正文 ({len(issue_body)} chars) ---")
        print(issue_body)
    for i, msg in enumerate(wecom_msgs, 1):
        print(f"\n--- WeCom 消息 {i}/{len(wecom_msgs)} ({len(msg.encode('utf-8'))}B) ---")
        print(msg)
    print(f"\n--- Telegram ({len(tg_msg)} chars) ---")
    print(tg_msg)
    print(sep)


async def run(
    cfg: dict,
    *,
    new_events: Optional[list[Event]] = None,
    items_by_event: Optional[dict] = None,
    situation: Optional[Situation] = None,
    site_url: str = "",
    dry_run: bool = False,
) -> None:
    """notify 子系统入口: main.py 三路汇合后统一调用"""
    notify_cfg = cfg.get("notify", {})
    if not notify_cfg.get("enabled", False):
        return

    state = load_notify_state()
    state = prune_fingerprints(state, notify_cfg.get("breaking", {}).get("cooldown_min", 30))

    tasks = decide(cfg, state, new_events or [], items_by_event or {})
    if not tasks:
        return

    from datetime import datetime
    from zoneinfo import ZoneInfo
    now_hkt_str = datetime.now(ZoneInfo("Asia/Hong_Kong")).strftime("%m月%d日 %H:%M")

    total_budget = notify_cfg.get("max_messages_per_run", 6)
    used_budget = 0

    client = MinimaxClient(model=cfg["minimax"]["model"])
    try:
        for task in tasks:
            remaining = total_budget - used_budget
            if remaining <= 0:
                logger.info("Notify message budget exhausted for this run")
                break
            try:
                used, success = await _execute_task(
                    task, cfg, client, situation, site_url, state,
                    dry_run, remaining, now_hkt_str,
                )
                used_budget += used
            except Exception as e:
                logger.error(f"Notify task {task.kind} failed: {e}")
                continue

            # 落去重键: 任一渠道成功才标记(全失败下轮重试)
            if success and not dry_run:
                if task.kind == KIND_MORNING:
                    state["morning_last_date"] = task.slot_key
                elif task.kind == KIND_WEEKLY:
                    state["weekly_last_slot"] = task.slot_key
    finally:
        await client.close()

    if not dry_run:
        save_notify_state(state)
