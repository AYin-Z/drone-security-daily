"""配置加载：YAML -> 简单 dict 对象，带默认值兜底。"""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml

DEFAULTS: dict = {
    "smtp": {
        "host": "smtp.qq.com",
        "port": 465,
        "user": "",
        "password": "",
        "from_name": "无人机感知与反制技术日报",
        "to": [],
        "attach_report_html": False,
        "dry_run": True,
    },
    "llm": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "",
        "model": "deepseek-chat",
        "temperature": 0.3,
        "timeout": 120,
    },
    "search": {
        "window_hours": 24,
        "min_articles": 6,
        "lookback_hours": 48,
        "max_candidates": 60,
        "keywords": [],
        "tavily_api_key": "",
        "tavily_max_results": 10,
        "rss_feeds": [],
        "sites": [],
        "wechat": {"manual_urls": []},
    },
    "runtime": {
        "send_email": True,
        "verbose": True,
        "timezone": "Asia/Shanghai",
    },
    "schedule": {"cron": "0 8 * * 1-5", "note": ""},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Config:
    def __init__(self, data: dict, root: Path):
        self.data = data
        self.root = root

    def __getitem__(self, key: str):
        return self.data[key]

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    @property
    def work_dir(self) -> Path:
        return self.root

    def path(self, *parts: str) -> Path:
        return self.root.joinpath(*parts)


def load_config(path: str | os.PathLike) -> Config:
    """加载配置文件；缺失字段用默认值兜底。"""
    p = Path(path)
    raw: dict = {}
    if p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"配置文件格式错误: {p}")
    merged = _deep_merge(DEFAULTS, raw)
    return Config(merged, p.resolve().parent)
