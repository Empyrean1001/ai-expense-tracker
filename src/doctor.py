"""Environment diagnostics: every failure names the exact command or action
that fixes it. Consumer-readiness means never showing a raw traceback for a
setup problem."""

import importlib
from dataclasses import dataclass
from pathlib import Path

from src.ollama_client import OllamaError, check_ollama_available


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


def run_doctor(config: dict, project_dir: Path) -> list[Check]:
    checks: list[Check] = []

    missing = []
    for module in ("pandas", "openpyxl", "pydantic", "yaml", "click", "httpx",
                   "langgraph", "langchain_ollama"):
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(module)
    checks.append(Check(
        "Python dependencies",
        not missing,
        "" if not missing else
        f"missing: {', '.join(missing)}.  Fix:  pip install -r requirements.txt",
    ))

    data_dir = project_dir / config.get("paths", {}).get("data_dir", "data")
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".write_probe"
        probe.write_text("ok")
        probe.unlink()
        checks.append(Check(f"Data directory writable ({data_dir})", True))
    except OSError as exc:
        checks.append(Check(
            f"Data directory writable ({data_dir})", False,
            f"cannot write: {exc}. Check permissions or set paths.data_dir in config.yaml",
        ))

    host = config["ollama"]["host"]
    model = config["ollama"]["model"]
    try:
        check_ollama_available(host, model)
        checks.append(Check(f"Ollama reachable at {host}, model '{model}' installed", True))
    except OllamaError as exc:
        checks.append(Check("Ollama", False, str(exc)))

    return checks
