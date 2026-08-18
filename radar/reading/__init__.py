"""深度解读阅读流 —— 与投研内参并行的第二条管道

问的不是"这跟哪只票有关", 而是"读完认知会不会变"。因此它有自己的一把尺子
(prompts/reading_triage.txt)、自己的摘要格式(prompts/reading_note.txt)、
自己的产物(reports/reading-*.md), 与判断链完全隔离。

分叉点在 main.collect_all() 的出口, 而不是从 archive 里挑:
投研 triage 把"评论与解读类"定在 4-6 分, 而 min_score_to_keep 是 5,
没过闸的条目根本不落盘 —— 想要的那类文章正好是被丢掉的那批。

条目只在被采集的那一轮可见(dedup 之后不再出现), 所以筛选必须与采集同轮发生,
选中的先入队, 每日到点再出清单。
"""
