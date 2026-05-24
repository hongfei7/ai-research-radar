# AI 投研雷达

> AI/科技/半导体板块 · 滚动情报库 · 由 MiniMax 驱动策展
> 仅作为研究输入素材，不构成投资建议

## 最新日报

- [2026-05-24 晨报](https://github.com/hongfei7/ai-research-radar/issues/1)
- [实时看板](https://hongfei7.github.io/ai-research-radar)
- [RSS 订阅](https://hongfei7.github.io/ai-research-radar/feed.xml)

---

---

## 系统概览

**AI 投研雷达**是一套全自动投研情报流水线，专为 AI/半导体赛道设计。系统每 20 分钟从 16 个数据源采集信息，通过 MiniMax LLM 进行两级智能处理——先以投资相关性评分过滤噪音，再以深度信号提取锁定关键变量——最终经由嵌入聚类将碎片化报道编织成结构化事件脉络，并通过 RSS、Web 看板、Telegram 与 GitHub Issues 四通道分发。

```
  arXiv · HN · GitHub Trending · SEC EDGAR · 10+ 全球科技媒体
        ╲              ╱
         ▼            ▼
      ┌───────────────────┐
      │   Collect & Dedup  │  原始数据采集 + 指纹去重
      └────────┬──────────┘
               ▼
      ┌───────────────────┐
      │   Triage (LLM)     │  投资相关性评分 0-10 · 噪音过滤
      └────────┬──────────┘
               ▼
      ┌───────────────────┐
      │   Extract (LLM)    │  中文摘要 · 标的映射 · 主题标签 · 方向研判
      └────────┬──────────┘
               ▼
      ┌───────────────────┐
      │   Cluster (Embed)  │  余弦相似度事件聚合 · 动态事件线
      └────────┬──────────┘
               ▼
      ┌───────────────────┐
      │   Situation (LLM)  │  滚动式市场态势综述
      └────────┬──────────┘
               ▼
      ┌───────────────────┐
      │   Render & Publish │  RSS + HTML + MD + Telegram
      └───────────────────┘
```

---

## 核心亮点

<table>
<tr>
<td width="50%">

### 智能信号提取

LLM 不只是摘要——每条信息经过投资相关性评分 (0-10)、关联到具体标的与主题、判断多空方向，并给出"So What"投资启示。所有输出严格校验，杜绝幻觉。

</td>
<td width="50%">

### 事件级视角

基于 Embedding 余弦相似度的自动聚类，将同一事件的分散报道编织为持续追踪的事件线。每个事件拥有独立时间轴、影响评估与当前状态。

</td>
</tr>
<tr>
<td>

### 全自动运维

GitHub Actions 驱动，每 20 分钟一个完整循环。采集→处理→聚类→分发全链路自动化，结果自动提交归档。零人工干预即可持续运行。

</td>
<td>

### 多渠道触达

RSS 订阅、交互式 HTML 看板、每日 Markdown 简报、Telegram 即时推送、GitHub Issues 归档——根据使用场景选择最佳信息消费方式。

</td>
</tr>
</table>

---

## 监控雷达

系统覆盖 **36 个标的**，横跨美股、港股、A 股与非上市实体，构建完整的 AI/半导体产业链监控矩阵。

<table>
<tr><th>市场</th><th>数量</th><th>标的</th></tr>
<tr>
<td align="center"><strong>美股</strong></td>
<td align="center">17</td>
<td>
  <sub>
  NVDA · AMD · AVGO · TSM · ASML · MU · INTC · MRVL · QCOM<br>
  ARM · SMCI · DELL · MSFT · GOOGL · AMZN · META · AAPL
  </sub>
</td>
</tr>
<tr>
<td align="center"><strong>港股</strong></td>
<td align="center">6</td>
<td>
  <sub>中芯国际 (0981) · 阿里巴巴 (9988) · 腾讯 (0700) · 百度 (9888) · 商汤 (0020) · 小米 (1810)</sub>
</td>
</tr>
<tr>
<td align="center"><strong>A 股</strong></td>
<td align="center">9</td>
<td>
  <sub>寒武纪 (688256) · 海光信息 (688041) · 工业富联 (601138) · 中科曙光 (603019)<br>浪潮信息 (000977) · 景嘉微 (300474) · 长电科技 (600584) · 金山办公 (688111) · 科大讯飞 (002230)</sub>
</td>
</tr>
<tr>
<td align="center"><strong>非上市</strong></td>
<td align="center">4</td>
<td>
  <sub>OpenAI · Anthropic · DeepSeek · xAI</sub>
</td>
</tr>
</table>

### 投资主题矩阵

| 主题 | 核心关注点 |
|------|-----------|
| `compute_demand` | 算力需求 | 云厂 Capex · Token 消耗 · 推理/训练需求弹性 |
| `chip_supply` | 芯片供给 | 先进制程 · 晶圆产能 · 良率 · 交付周期 |
| `advanced_packaging` | 先进封装与 HBM | CoWoS 产能 · HBM 定价 · 封装技术路线 |
| `model_capability` | 模型能力曲线 | 前沿模型性能 · 效率突破 · Scaling Law 演进 |
| `ai_monetization` | AI 应用与变现 | AI 产品收入 · 商业化进展 · 付费率 |
| `edge_ai` | 终端 AI | AI 手机/PC/眼镜 · 端侧推理芯片 · 隐私计算 |
| `datacenter_power` | 数据中心与能源 | 数据中心建设 · 电力供给约束 · 散热技术 |
| `policy_export` | 政策与出口管制 | 出口管制升级/松绑 · 补贴政策 · 监管动态 |
| `domestic_substitution` | 国产替代 | 国产算力链替代进程 · 自主可控进展 |

---

## 数据源矩阵

### 学术与社区
| 数据源 | 类型 | 说明 |
|--------|------|------|
| arXiv | API | cs.AI / cs.CL / cs.LG 分类，每日最新论文 |
| Hacker News | API | Top Stories，72h 时间窗口 |
| GitHub Trending | 爬虫 | 每日热门仓库 |
| Lobsters | RSS | 技术社区热帖 |

### 官方与科技媒体
| 数据源 | 类型 | 说明 |
|--------|------|------|
| OpenAI Blog | RSS | 官方动态 |
| Google AI Blog | RSS | 官方研究 |
| NVIDIA Blog | RSS | 官方技术博客 |
| Microsoft Research | RSS | 微软研究院 |
| The Verge AI | RSS | AI 专线 |
| TechCrunch AI | RSS | AI 创投 |
| Ars Technica | RSS | 深度科技 |
| Wired | RSS | 科技文化 |
| MIT Tech Review | RSS | 前沿科技 |
| VentureBeat AI | RSS | AI 产业 |
| IEEE Spectrum AI | RSS | 工程视角 |

### 监管与财务
| 数据源 | 类型 | 说明 |
|--------|------|------|
| SEC EDGAR | API | 8-K 重大事项申报，覆盖全部 US 标的 |

---

## 项目结构

```
ai-research-radar/
│
├── main.py                     # 流水线编排 — 5 个运行阶段的主控制器
├── config.yaml                 # 系统配置 — 标的、数据源、评分/聚类阈值
├── requirements.txt            # 依赖清单
│
├── radar/                      # 核心引擎
│   ├── models.py               #   数据模型：Item / Event / Situation
│   ├── config.py               #   配置加载、校验、Prompt 格式化
│   ├── minimax_client.py       #   MiniMax API 客户端 (Chat + Embedding)
│   ├── processor.py            #   双层 LLM 处理管线 (Triage + Extract)
│   ├── cluster.py              #   事件聚类引擎 (Embedding + 相似度匹配)
│   ├── situation.py            #   态势综述生成器
│   ├── dedup.py                #   指纹去重 (SQLite)
│   ├── credibility.py          #   信源可信度评级 (红绿灯体系)
│   ├── storage.py              #   持久化层 (JSONL 归档)
│   ├── render.py               #   输出渲染 (Jinja2)
│   ├── publish.py              #   分发引擎 (GitHub · Telegram · RSS)
│   └── collectors/             #   采集器模块
│       ├── base.py             #     Collector 抽象基类
│       ├── rss.py              #     RSS/Atom 通用采集器
│       ├── arxiv.py            #     arXiv API 采集器
│       ├── hackernews.py       #     Hacker News Firebase API
│       ├── github_trending.py  #     GitHub Trending HTML 解析
│       └── sec_edgar.py        #     SEC EDGAR 监管文件采集
│
├── prompts/                    # LLM Prompt 模板
│   ├── triage.txt              #   投资相关性评分
│   ├── extract.txt             #   深度信号提取
│   ├── cluster.txt             #   事件合并重写
│   └── situation.txt           #   态势综述生成
│
├── templates/                  # Jinja2 输出模板
│   ├── feed.xml.j2             #   RSS 2.0 Feed
│   ├── dashboard.html.j2       #   交互式 Web 看板
│   ├── brief.md.j2             #   每日投研简报
│   └── ticker.html.j2          #   单票详细视图
│
├── archive/                    # 每日 JSONL 归档
├── state/                      # 运行时状态 (DB, events, situation)
├── pages/                      # 静态输出 (GitHub Pages)
└── .github/workflows/          # CI/CD 自动化
    ├── daily.yml               #   定时运行 (每 20 分钟)
    └── pages.yml               #   GitHub Pages 部署
```

---

## 快速开始

### 前置条件

- Python 3.11+
- [MiniMax API Key](https://platform.minimaxi.com/)
- (可选) Telegram Bot Token + Chat ID
- (可选) GitHub Token (用于 Issues 自动创建)

### 安装

```bash
git clone <repo-url> && cd ai-research-radar
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 环境变量

```bash
export MINIMAX_API_KEY="sk-xxxxxxxx"
export TELEGRAM_BOT_TOKEN="123:abc"        # 可选
export TELEGRAM_CHAT_ID="-100xxxxxxxx"     # 可选
export GITHUB_TOKEN="ghp_xxxxxxxx"         # 可选
```

### 运行流水线

```bash
python main.py --stage collect   # Phase I   仅采集
python main.py --stage process   # Phase II  采集 + LLM 两阶段处理
python main.py --stage cluster   # Phase III 采集 + 处理 + 事件聚类
python main.py --stage full      # Phase IV  完整流水线 + 渲染 + 分发
```

---

## 输出矩阵

| 频道 | 路径/方式 | 刷新频率 | 适用场景 |
|------|----------|----------|---------|
| **RSS Feed** | `pages/feed.xml` | 每 20 分钟 | RSS 阅读器订阅，自动化消费 |
| **Web 看板** | `pages/index.html` | 每 20 分钟 | 浏览器浏览，可视化监控 |
| **每日简报** | `pages/brief-YYYY-MM-DD.md` | 每日 | 晨会阅读，结构化投研笔记 |
| **Telegram** | Bot 推送 | 事件驱动 | 即时告警，新重大事件即刻触达 |
| **GitHub Issues** | 仓库 Issue | 每日 07:00 HKT | 归档检索，回溯历史简报 |

---

## 核心配置

> 所有参数集中在 `config.yaml`，无需修改代码。

| 参数路径 | 默认值 | 语义 |
|----------|--------|------|
| `runtime.cron_interval_minutes` | `20` | 流水线运行间隔 |
| `runtime.rolling_window_hours` | `24` | 事件滚动时间窗 |
| `runtime.situation_update_interval` | `3` | 态势重写间隔 (轮次) |
| `scoring.min_score_to_keep` | `6` | 相关性最低保留分 (0-10) |
| `scoring.max_items_in_brief` | `25` | 简报最大条目数 |
| `clustering.similarity_threshold` | `0.85` | Embedding 聚类相似度阈值 |
| `clustering.max_active_events` | `30` | 同时追踪的最大活跃事件数 |
| `clustering.event_ttl_hours` | `24` | 无更新事件自动归档时间 |
| `minimax.model` | `MiniMax-M2.7` | 使用的 LLM 模型 (Coding Plan) |

---

## 架构原则

- **配置驱动**：所有标的、数据源、阈值均从 `config.yaml` 读取，零硬编码
- **幻觉防御**：LLM 输出的标的 Ticker 和主题标签均经配置文件校验，自动剔除无效值
- **幂等设计**：采集和存储层均支持重复运行，基于指纹去重保证数据一致性
- **模块化采集**：所有采集器实现统一 `Collector` 接口，新增数据源只需实现 `fetch()` 方法
- **渐进式管线**：支持分阶段运行 (`collect` → `process` → `cluster` → `full`)，便于调试和资源控制

---

## 信源可信度体系

系统为每个数据源赋予可信度评级，影响其在看板中的展示权重：

| 等级 | 标识 | 典型数据源 |
|------|------|-----------|
| **High** | 🟢 | SEC EDGAR、官方博客 |
| **Medium** | 🟡 | arXiv、主流科技媒体 |
| **Low** | 🔴 | 技术社区 (HN、Lobsters) |

---

<p align="center">
  <sub>Built for AI/Semiconductor investors who need signal, not noise.</sub>
</p>
