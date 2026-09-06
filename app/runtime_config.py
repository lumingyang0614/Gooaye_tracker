from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "settings.yaml"


@lru_cache
def load_runtime_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        value = yaml.safe_load(handle) or {}
    if not isinstance(value, dict):
        raise ValueError("config/settings.yaml must contain a YAML object")
    return value
