from pathlib import Path
from typing import Any

import yaml


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path).resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle) or {}

    config["_config_dir"] = str(config_path.parent)
    config.setdefault("database_path", "data/castings.sqlite3")
    config.setdefault("request_timeout_seconds", 20)
    config.setdefault("user_agent", "CastingWatchMVP/0.1")
    config.setdefault("request_delay_seconds", 1.0)
    if not config.get("search"):
        config["search"] = {}
    config.setdefault("sources", [])
    return config


def resolve_data_path(config: dict[str, Any], key: str) -> Path:
    value = Path(config[key])
    if value.is_absolute():
        return value
    return Path(config["_config_dir"]) / value
