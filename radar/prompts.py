"""统一 prompt 模板加载"""

from pathlib import Path

from radar.models import today_str

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def load_prompt(name: str) -> str:
    """加载 prompt 模板文件（.txt），不存在时抛出 FileNotFoundError。

    模板里的 {current_date} 在此统一替换为当前 HKT 日期 —— 不注入当前日期时
    LLM 会按训练语料的时间感写作（实测产出过"影响 2024H2 算力供给"这类错年份
    表述）。这一步先于调用方的 .format()/.replace()，替换后模板中不再有该占位符。
    """
    path = _PROMPTS_DIR / f"{name}.txt"
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8").replace("{current_date}", today_str())
