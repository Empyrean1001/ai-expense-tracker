"""Cache of learned BankProfiles, keyed by a fingerprint of the header row.

A cache hit means next month's statement from the same bank parses with zero
LLM calls. Profiles are human-readable JSON and safe to hand-edit — they
describe a layout, never actual transactions.
"""

import hashlib
import json
from pathlib import Path

from src.models import BankProfile
from src.profile import find_header_row, normalize_cell


def fingerprint(header_signature: list[str]) -> str:
    key = "|".join(normalize_cell(cell) for cell in header_signature)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def save_profile(profiles_dir: Path, profile: BankProfile) -> Path:
    profiles_dir.mkdir(parents=True, exist_ok=True)
    path = profiles_dir / f"{fingerprint(profile.header_signature)}.json"
    path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")
    return path


def load_profiles(profiles_dir: Path) -> list[BankProfile]:
    if not profiles_dir.exists():
        return []
    profiles = []
    for path in sorted(profiles_dir.glob("*.json")):
        profiles.append(BankProfile(**json.loads(path.read_text(encoding="utf-8"))))
    return profiles


def match_profile(profiles_dir: Path, rows: list[list[str]]) -> BankProfile | None:
    """The first cached profile whose header signature appears in the file."""
    for profile in load_profiles(profiles_dir):
        if find_header_row(rows, profile.header_signature) is not None:
            return profile
    return None
