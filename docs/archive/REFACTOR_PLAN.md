# AI 投研雷达 — 推送系统完全重构计划 (v3 定稿)

> 状态： ✅ 两轮审计完成。轮次 1(4 严重/4 高/5 中）→ v2 闭环；轮次 2 复审（1 阻断/2 高/6 中低）→ v3 全部闭环。可执行。

## 〇、审计轮次 2 重大发现： 现网 CI bug（先于重构必须修）

**CI 提交循环自 2026-05-28 起静默丢弃所有已跟踪文件的 state 变更。**

daily.yml:34-36 顺序为 `fetch → reset --hard origin/main → add → commit`,`reset --hard` 丢弃本轮管道对 tracked 文件的全部修改。git 历史证据： `state/` 最后提交停在 7ad5d10(2026-05-28),`situation.json` 的推送时间戳冻结在 5/28;8 月的 radar 提交只含新增 untracked 文件（archive jsonl、新 ticker 页）。

后果： 事件状态/态势/推送去重键每轮都在冻结基座上重建，跨轮连续性已断。这不仅阻断重构（notify_state.json 第二日起无法持久化 → 晨报每天重发 4-5 次），更是正在发生的现网故障。

**→ 新增 S0.5（最高优先级，先于一切）**: 提交循环改为 `add → commit → fetch → rebase → push`（失败重试 rebase)。

## 一、现状诊断

- `radar/publish.py` 908 行，WeCom/Telegram 两套格式化逻辑重复且不一致
- **内容根因 1**: 推送 = Event 字段机械截断拼接（标题 40 字/摘要 50 字/分析 40 字）
- **内容根因 2**: 晨报 `render_daily_brief` 只收到触发轮 15 分钟的条目 (main.py:505) —— "24h 晨报"名不副实
- **节奏混乱**: 15min 轮询 + sig 阈值 + 2h 兜底 + 静默时段四套规则叠加
- **晨报链路**: main.py:522 → `send_wecom_brief` → `send_wecom_news`（活代码）;`write_daily_brief`（死代码）
- **状态字段腐化**: `Situation.morning_brief_date` 注释与实际语义不符 (models.py:148)
- **CI state 持久化失效**: 见 §〇

## 二、用户决策（已确认）

完全重写输出层（企业微信实时推送 + 晨报 + Issue 正文）,LLM 撰稿，深度研报风，晨报等产品形态重新构思。

## 三、保留 vs 重写

**保留**: collectors、dedup、processor、cluster、situation（审计 F2: 灰度期不动其字段拷贝逻辑）、storage、render.py 看板/RSS/ticker 页

**重写**: publish.py → `radar/notify/` 子系统；brief.md.j2;main.py 分发逻辑；新增 `prompts/notify_*.txt`

**灰度期新旧并存**: `notify.use_legacy` 开关；旧代码 S8（稳定 ≥7 天后）才删除

## 四、产品定义

### P1. 晨报（07:00 HKT，宽限 06:45-08:00，过窗补发并标注延迟）
LLM 基于**过去 24h 装配层数据**撰稿： ①核心观点（3-5 条） ②要闻回顾（按主线分组） ③重点关注 ④风险与分歧
- 投递： WeCom(section 拆分多条） + GitHub Issue 全文存档（幂等： 先查当日 Issue) + Telegram(headline+核心观点截取，不加 LLM 调用）
- 不受静默时段约束
- 空素材： 仍发，降级为"事件线延续状态 + 无新增声明";prompt 注入"禁止编造"硬约束
- **审计 R2 对策**: 装配层不按纯 significance 截断 —— "风险与分歧"素材（second_opinion/低可信条目）单独保留配额，不进入 sig 降序裁剪池
- 取消 news 图文卡片

### P2. 定时速递（12:30 / 18:00 HKT，宽限 45min，过窗则跳过不补发 —— 审计 F6)
- 精简版： 核心变化（≤3) + 事件更新 + 一句话态势
- 空素材： 静默不发

### P3. 突发快讯（即时）
- 触发： 本轮新事件 sig ≥ 8；每轮硬上限 3 条（sig 降序）
- **溢出路由（审计 F4 修正）**: 超出 3 条的并入**下一轮快讯评估**(15min 后）而非定时速递
- LLM 三句结构： 发生了什么 / 为什么重要 / 接下来关注什么
- **防重指纹 = 内容签名（审计 S3+F9)**: 主 ticker 集合 + **首条原始条目标题**（不用 event 级 LLM 标题，避免 `_rewrite_event` 标题漂移）bigram 哈希，冷却 30min 内 Jaccard ≥ 0.6 视为同一突发；复用 `cluster.py:31-51` 的 `_tokenize_for_matching`
- 静默时段（01:00-07:00）仅 sig ≥ 9 破例

### 取消项
2h 兜底、字段拼接 formatter、news 卡片、"目前关注"罗列

## 五、架构设计

```
radar/notify/
├── types.py        # DigestPayload 结构化稿件(dataclass, channel-agnostic)
├── assemble.py     # 数据装配层: 按时间窗从 events.json + archive/*.jsonl 加载素材
│                   #   token 预算 ≤8k(按 sig 降序截断, 风险素材单独配额)
├── copywriter.py   # LLM 撰稿: 自持 MinimaxClient 生命周期(审计 F3)
│                   #   失败矩阵: asyncio.wait_for ≤90s 超时 / 空输出 / schema 不合规 / 异常
│                   #   / API key 缺失(请求时才炸, minimax_client.py:44-47)
│                   #   → 统一降级兜底模板稿, 走同一渲染器
├── render_wecom.py # → WeCom markdown 多条(版式规范: 无 # / 无 backtick / 无表格;
│                   #   **加粗** + > 引用 + <font> 三色; section 内超 3800B 段落二次拆分+"(续)")
├── render_telegram.py # → Telegram HTML parse_mode, 4096 字符预算
├── transport.py    # httpx 发送 + 每轮全局 ≤6 条消息预算(快讯优先, 长文超预算合并为汇总条)
└── scheduler.py    # 推送决策: 目标时点+宽限期+当日去重键
state/notify_state.json  # 去重键(日期键/内容指纹)
prompts/notify_{morning,digest,breaking}.txt
```

### main.py 接入方式（审计 F3 具体化）
- 消除两条 early return(main.py:317 / :348)：三路汇合为"处理管道 → 统一渲染 → `await notify.run(cfg)`"
- notify 子系统**自持 MinimaxClient**（空路径原本无 client);client 创建/关闭在 `notify.run` 内
- 上提重复渲染代码为单次调用

### 状态管理（审计 H2+F2+F5 修正）
- S1 仅**新增** notify_state 读写；灰度期旧字段（`last_wecom_digest_at` 等）**双写保留**,situation.py:165-167 不动 —— 保证 `use_legacy` 回滚真实可用
- 旧字段删除并入 S8（与旧代码删除同步）
- 不幻想防重发： state 丢失即可能重发（WeCom/TG 无幂等）,Issue 侧幂等兜底；S0.5 修复后 state 丢失概率大幅降低

### 灰度关停矩阵（审计 F8)
| 新产品上线 | 同步关停旧逻辑 |
|---|---|
| P1 晨报 | 旧 Issue 晨报分支 + `send_wecom_brief`(news 卡） + 晨报 TG 全文 |
| P2 速递 | `_wecom_fallback_push` + TG/WeCom 兜底间隔推送 |
| P3 快讯 | `should_wecom_alert`/`should_telegram_alert` 阈值触发 |

## 六、实施步骤

- **S0** 定稿本计划（本文档）
- **S0.5** ⚠️ **修复 daily.yml 提交循环**(add→commit→fetch→rebase→push 重试）,**独立提交先于重构上线**
- **S1** `notify/types.py` + `assemble.py` + `notify_state.json` 读写（双写，不动旧字段）+ **config.yaml `notify:` schema**(use_legacy/products 开关/时点/阈值/冷却/预算，审计 F7) + `load_config` 校验
- **S2** `copywriter.py` + 3 个撰稿 prompt(JSON schema 校验 + 失败矩阵 + 兜底模板稿）
- **S3** `render_wecom.py` + `render_telegram.py` + `transport.py`（消息预算/二次拆分）
- **S4** `scheduler.py` + main.py 三路汇合重构（消 early return、notify 自持 client、`use_legacy` 并存）
- **S5** `--notify-dry-run` + `tests/test_notify.py`（字节预算/UTF-8 边界/二次拆分/指纹去重/调度窗口含 Actions 延迟模拟/指纹阈值用真实数据校准）—— **实现审计 = 验收门槛**
- **S6** 新晨报 Issue 模板 + Issue 幂等 + README
- **S7** 灰度： 按 §五关停矩阵逐产品上线（晨报 3 天 → 速递 → 快讯）
- **S8** 稳定 ≥7 天后： 删除旧 publish.py 推送函数、`send_wecom_brief/news`、`write_daily_brief`、Situation 旧字段及 situation.py 拷贝逻辑、`render_daily_brief` 迁移

## 七、审计记录

### 轮次 1 — 设计对抗审计 ✅
4 严重（调度挂接点/数据来源/event_id 防重/消息预算） + 4 高（时点命中/状态双写/LLM 失败/空素材） + 5 中（事实修正） → v2 闭环

### 轮次 2 — 独立复审 ✅
闭环验证： S3/S4/H1/H3/H4/M 系完全闭环；S1/S2 设计闭环但暴露实现层缺口；H2 被新发现击穿
新发现 → v3 闭环：
- F1（阻断） CI state 丢弃 → §〇 S0.5
- F2（高） 字段清理时序摧毁回滚 → §五 双写+清理入 S8
- F3（高） 空路径无 LLM client → §五 notify 自持 client + main.py 汇合具体化
- F4 快讯溢出路由矛盾 → §四-P3 改并下一轮快讯评估
- F5 防重发推理错误 → §五 如实表述 at-least-once
- F6 速递过窗未定义 → §四-P2 过窗跳过
- F7 缺 config schema 步骤 → S1
- F8 灰度双发 → §五 关停矩阵
- F9 指纹素材漂移 → §四-P3 用首条原始标题

### 实现审计（S5)✅ 已完成（2026-08-12)
- 20 个单元测试全绿（字节预算/UTF-8 边界/二次拆分/指纹去重/调度窗口含 Actions 延迟模拟）
- **测试抓出 2 个实现 bug**: ① `_split_oversize_block` 单行截断死循环（截断后加省略号仍超预算）;② 调度器"晨报补发"文案错别字
- 真实数据 dry-run 全链路验证： 调度（晨报补发）→ 装配（5 条风险素材）→ LLM 失败矩阵（本地无 API key)→ 兜底稿 → 双渠道渲染，按设计工作
- ticker 页大小写归一（config 规范名/别名映射）落地于 render.py,`ARM/Arm`、`Meta/META` 重复页已清理
- 遗留： LLM 真实撰稿质量需灰度首日人工审查（本地无 MINIMAX_API_KEY 未实测 LLM 路径）; 指纹阈值 0.6 待真实数据校准

## 八、验证方案

1. S0.5 验证： CI 跑一轮后 `git log -- state/` 出现新提交、situation.json 时间戳更新
2. 单测 `tests/test_notify.py`（见 S5)
3. dry-run 真实 state 人工审查三类稿件质量
4. 灰度观察： 无重复推送、无刷屏、无缺席
5. 回滚： `notify.use_legacy: true`(S8 前始终可用）

## 九、残留风险（已评估可接受）

- LLM 成本： MiniMax-M2.7 为配额制订阅 (config.yaml:165)，现有管道已主动最大化配额使用，每日 5-10 次 ≤8k token 额外调用可忽略
- 指纹阈值 0.6:S5 用真实数据校准
- 消息排队： ≤6 条/轮 vs WeCom 20 条/min，余量充足
- events.json 无界增长（11307 行）: 装配层每轮全量 load 长期拖慢，低优先级，后续可加归档清理
