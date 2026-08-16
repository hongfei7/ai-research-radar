# Sterling 证券研究 · AI 首席内参

> AI / 半导体板块 · 全自动投研情报流水线 · 由 MiniMax 驱动
> 仅作为研究输入素材，不构成投资建议

<!-- INDEX:START -->

## 最新内参

- [2026-08-17 内参](reports/2026-08-17.md) · [Issue](https://github.com/hongfei7/ai-research-radar/issues/49)
- [2026-08-16 内参](reports/2026-08-16.md) · [Issue](https://github.com/hongfei7/ai-research-radar/issues/46)
- [2026-08-15 内参](reports/2026-08-15.md) · [Issue](https://github.com/hongfei7/ai-research-radar/issues/45)
- [2026-08-14 内参](reports/2026-08-14.md) · [Issue](https://github.com/hongfei7/ai-research-radar/issues/43)
- [2026-08-13 内参](reports/2026-08-13.md) · [Issue](https://github.com/hongfei7/ai-research-radar/issues/37)

<!-- INDEX:END -->

## 这是什么

每 15 分钟从 30 余个信源采集 AI / 半导体相关信息，经两级 LLM 处理（投资相关性筛选 → 深度信号提取）压成事件线，每日 07:00 HKT 产出一份「AI 首席内参」。

报告不是新闻摘要，而是**判断链**：每期给出 3-4 条编号判断，每条按 `事实 → 机理 → 推论 → 证伪条件` 展开，并附带可点击的证据来源。写下的证伪条件会在下一期被逐条回溯核对——这是它区别于「每天重新发明一遍观点」的自动摘要的地方。

```
30+ 信源 ── 采集 ── 去重 ── Triage ── Extract ── 聚类 ── 态势
                                                          │
                                              ┌───────────┴───────────┐
                                              │   notify 子系统        │
                                              │  装配 → 撰稿 → 渲染     │
                                              └───────────┬───────────┘
                                          ┌───────────────┼───────────────┐
                                     GitHub Issue    reports/*.md    WeCom / TG
```

## 报告结构

四个层次，从宏观到微观再到时间维度：

| 章节 | 作用 |
|------|------|
| **格局** | 跨期演化的宏观框架：周期位置 / 当前主约束 / 较上期是维持、微调还是转向 |
| **本期速览** | 一张表看完全部判断与各自的验证点 |
| **上期判断回溯** | 逐条核对上期的证伪条件：已验证 / 进展中 / 接近证伪 / 已证伪 |
| **判断一…N** | 事实 → 机理 → 推论 → 证伪条件 → 证据链接 → 反面观点 |
| **张力与联动** | 判断之间互相强化还是互相矛盾 |
| **未进入判断的观察** | 值得知道但不值得展开的事件 |
| **附录** | 事件线数据表：主标的 / 信源数 / 首报时间 / 原文链接 |

**证据不可能是幻觉链接**：喂给撰稿 LLM 的素材已剥去全部 URL，它只能用 `E1`、`E2` 这样的编号引用事件；渲染层再把编号还原成链接。LLM 看不到链接，也就编不出链接。

## 运行

需要 Python 3.11+ 和 [MiniMax API Key](https://platform.minimaxi.com/)。

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MINIMAX_API_KEY="sk-xxxx"
```

可选环境变量：`TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` / `WECOM_WEBHOOK_URL` / `GITHUB_TOKEN`。

```bash
python main.py --stage collect             # 仅采集 + 去重 + 归档
python main.py --stage process             # + LLM 两级处理
python main.py --stage cluster             # + 事件聚类
python main.py --stage full                # 完整流水线（含分发）
python main.py --stage notify --notify-dry-run   # 只撰稿，打印到 stdout，不发送不写状态
```

`--notify-dry-run` 会打印 Issue 正文全文、微信分条消息和 Telegram 消息，是调版式最快的方式。

## 输出

| 载体 | 位置 | 说明 |
|------|------|------|
| GitHub Issue | 仓库 Issues，标签 `晨报` / `周报` | 完整正文，当日幂等（同题不重复创建） |
| Markdown 归档 | `reports/YYYY-MM-DD.md` | 与 Issue 同稿，带 frontmatter，可检索可 diff |
| 企业微信 | 群机器人 Webhook | 速览 + 每条判断的结论与证伪条件，按字节预算拆分多条 |
| Telegram | Bot 推送 | 单条 HTML，每条判断挂一个证据链接 |

## 数据源

41 个信源，按可信度分三档，直接影响条目进入深度处理的分数门槛。

- **高可信** — SEC EDGAR 8-K、arXiv、OpenAI / Google AI / DeepMind / NVIDIA / MSR / AWS ML 官方博客、IEEE Spectrum
- **中可信** — The Verge、TechCrunch、Ars Technica、Wired、MIT TR、VentureBeat、Tom's Hardware、ZDNet、EE Times、InfoQ、HuggingFace（Daily Papers + Blog）
- **中可信 · 半导体与战略分析** — Semiconductor Engineering、Stratechery、Interconnects、Simon Willison
- **中可信 · 中文** — 36氪、雷锋网、量子位、极客公园、钛媒体、爱范儿
- **低可信** — Hacker News、Reddit、GitHub Trending、Techmeme、MiniMax 搜索、DuckDuckGo 搜索

> 批量发布的信源（arXiv 每日批次、SEC 申报日期只精确到天、HF Daily Papers）在 `params.window_hours` 里单独放宽时间窗。用统一的 8 小时窗口会把它们整体过滤掉，只剩下伪造 `published_at` 的搜索类信源能通过。
>
> 部分 feed 会一次吐出整个历史归档（openai.com 单次 1100+ 条），用 `params.max_entries` 收紧。
>
> 已验证不可用、未纳入：Anthropic（无公开 RSS）、机器之心（需登录）、Datawhale（无 feed，且内容为开源教程，投研相关性低）、SemiAnalysis（免费 feed 自 2025-09 停更）。

## 模型

`MiniMax-M3`（1M 上下文，支持关闭推理）。推理开关按任务分档：

| 任务 | 推理 | 理由 |
|------|------|------|
| triage（40 条批量打分） | 关 | 延迟代价远大于质量收益 |
| extract（并发 5 路抽取） | 关 | 保吞吐 |
| 事件标题/摘要改写 | 关 | 机械任务 |
| 内参撰稿 | **开** | 判断链的「机理」段是报告立身之本 |

推理内容若内联在 `content` 中（未开 `reasoning_split` 时用 `<think>` 包裹），客户端在 JSON 解析前统一剥离。

若 M3 未在旧路由 `chatcompletion_v2` 上供给，无需改代码，切两个环境变量即可：

```bash
export MINIMAX_BASE_URL="https://api.minimax.io/v1"
export MINIMAX_CHAT_PATH="/chat/completions"
```

## 覆盖标的

36 个，横跨美股、港股、A 股与非上市实体。

| 市场 | 标的 |
|------|------|
| 美股 | NVDA · AMD · AVGO · TSM · ASML · MU · INTC · MRVL · QCOM · ARM · SMCI · DELL · MSFT · GOOGL · AMZN · META · AAPL |
| 港股 | 中芯国际 · 阿里巴巴 · 腾讯 · 百度 · 商汤 · 小米 |
| A 股 | 寒武纪 · 海光信息 · 工业富联 · 中科曙光 · 浪潮信息 · 景嘉微 · 长电科技 · 金山办公 · 科大讯飞 |
| 非上市 | OpenAI · Anthropic · DeepSeek · xAI · 天数智芯 |

投资主线九条：算力需求、芯片供给、先进封装与 HBM、模型能力曲线、AI 应用与变现、终端 AI、数据中心与能源、政策与出口管制、国产替代。

## 项目结构

```
main.py                     流水线编排（采集 → 处理 → 聚类 → 态势 → 分发）
config.yaml                 标的、信源、阈值——所有参数集中于此
radar/
  models.py                 Item / Event / Situation
  config.py                 配置加载与校验
  minimax_client.py         MiniMax Chat / 图片理解客户端
  processor.py              两级 LLM 处理 + 交叉分析 / 趋势 / 反向观点 / 深度分析
  cluster.py                事件聚类（关键词 + Jaccard；标的频次收敛、重要性重算）
  situation.py              滚动态势综述
  textnorm.py               标题清洗、句子边界裁剪、markdown 剥离
  credibility.py            信源可信度分级
  dedup.py / storage.py     URL 指纹去重（SQLite）/ JSONL 归档
  collectors/               8 类采集器，统一 Collector 接口
  notify/
    scheduler.py            决定本轮发什么（内参 / 复盘 / 快讯 + 防重指纹）
    assemble.py             按时间窗装配素材，生成 ref 与证据清单
    copywriter.py           LLM 撰稿 → DigestPayload，失败降级为兜底稿
    types.py                稿件契约（MacroFrame / DigestCall / CallReview）
    render_issue.py         判断链版式的完整 Markdown
    render_wecom.py         企业微信（字节预算拆分）
    render_telegram.py      Telegram HTML
    readme_index.py         README 索引区重建
    transport.py            WeCom / Telegram / GitHub Issue 发送
prompts/                    全部 LLM prompt（纯文本，改文案不动代码）
reports/                    每日内参归档
archive/                    每日 JSONL 原始归档
state/                      事件线、态势、推送去重键、已见指纹
```

## 设计约束

- **配置驱动** — 标的、信源、阈值全部来自 `config.yaml`，代码里零硬编码
- **幻觉防御** — LLM 输出的 ticker 与 theme 经配置白名单校验；证据 ref 经素材校验；URL 从不经过 LLM
- **重要性不通胀** — 事件重要性由成员条目最高分加多源加成算出，可升可降，不采纳 LLM 自报的分数
- **标的不膨胀** — 事件标的按命中频次收敛（要求至少出现在 1/3 来源中，上限 4 个），不做集合并
- **降级可见** — 撰稿失败时产出只有事实层的降级稿，并在报头显式标注，不伪装成正常内参
- **幂等** — 采集、归档、Issue 创建、README 索引重复执行结果一致

---

<sub>Built for AI/Semiconductor investors who need signal, not noise.</sub>
