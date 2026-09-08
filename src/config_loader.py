"""Loaders for the user-editable YAML files."""

from pathlib import Path

import yaml

from src.models import Rule


def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_categories(path: Path) -> dict[str, list[str]]:
    """Category -> list of subcategories."""
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_rules(path: Path) -> list[Rule]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return [Rule(**entry) for entry in (raw.get("rules") or [])]


def load_locale_hints(locales_dir: Path, code: str) -> str:
    """The hint text appended to categorization prompts, '' if no pack."""
    path = locales_dir / f"{code}.yaml"
    if not path.exists():
        return ""
    with open(path, encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("hints", "")
