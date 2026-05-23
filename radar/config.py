"""读取并校验 config.yaml"""

import os
import yaml
from pathlib import Path
from typing import Any

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(path: str | Path | None = None) -> dict:
    """加载配置文件，返回校验后的 dict"""
    path = Path(path) if path else _DEFAULT_CONFIG_PATH
    if not path.exists():
        raise FileNotFoundError(f"config.yaml not found at {path}")

    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    _validate(cfg)
    return cfg


def _validate(cfg: dict) -> None:
    """校验配置结构，缺失必要字段时抛出 ValueError"""
    # 必需顶层字段
    for key in ["coverage", "themes", "sources", "scoring", "minimax", "output", "runtime", "clustering", "channels"]:
        if key not in cfg:
            raise ValueError(f"config.yaml missing required key: {key}")

    # coverage 校验
    for entry in cfg["coverage"]:
        if "name" not in entry:
            raise ValueError("Each coverage entry must have 'name'")

    # themes 校验
    for entry in cfg["themes"]:
        if "key" not in entry or "name" not in entry:
            raise ValueError("Each theme must have 'key' and 'name'")

    # sources 校验
    for src_type in ["tech", "market"]:
        if src_type not in cfg["sources"]:
            raise ValueError(f"sources missing '{src_type}' key")
        for src in cfg["sources"][src_type]:
            if "id" not in src or "type" not in src:
                raise ValueError(f"Each source must have 'id' and 'type': {src}")

    # scoring 校验
    if "min_score_to_keep" not in cfg["scoring"]:
        raise ValueError("scoring must have 'min_score_to_keep'")

    # minimax 校验
    if "model" not in cfg["minimax"]:
        raise ValueError("minimax must have 'model'")


def get_coverage_names(cfg: dict) -> list[str]:
    """返回覆盖标的 name 列表"""
    return [c["name"] for c in cfg["coverage"]]


def get_coverage_by_name(cfg: dict, name: str) -> dict | None:
    """按 name 查找覆盖标的"""
    for c in cfg["coverage"]:
        if c["name"] == name:
            return c
    return None


def get_coverage_by_ticker(cfg: dict, ticker: str) -> dict | None:
    """按 ticker 查找覆盖标的"""
    for c in cfg["coverage"]:
        if c["ticker"].upper() == ticker.upper():
            return c
    return None


def get_theme_keys(cfg: dict) -> list[str]:
    """返回投资主线 key 列表"""
    return [t["key"] for t in cfg["themes"]]


def get_theme_by_key(cfg: dict, key: str) -> dict | None:
    """按 key 查找投资主线"""
    for t in cfg["themes"]:
        if t["key"] == key:
            return t
    return None


def format_coverage_for_prompt(cfg: dict) -> str:
    """生成注入 prompt 的覆盖标的清单文本"""
    lines = []
    for c in cfg["coverage"]:
        aliases_str = f"（别名: {', '.join(c['aliases'])}）" if c.get("aliases") else ""
        ticker_str = f" [{c['ticker']}]" if c["ticker"] else " [未上市]"
        lines.append(f"- {c['name']}{ticker_str} {aliases_str}")
    return "\n".join(lines)


def format_themes_for_prompt(cfg: dict) -> str:
    """生成注入 prompt 的投资主线清单文本"""
    lines = []
    for t in cfg["themes"]:
        lines.append(f"- {t['key']}: {t['name']}（{t['desc']}）")
    return "\n".join(lines)
