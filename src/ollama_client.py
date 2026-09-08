"""Tier 2 of the cascade: the local LLM call, plus the Ollama pre-flight.

Infrastructure failures (dead server, missing model) abort up-front with the
exact fix command; per-item inference failures degrade to the review queue
(decision D-14). `model` is anything with `.invoke(prompt) ->
CategorizationResult` — exactly what
`ChatOllama(...).with_structured_output(CategorizationResult)` returns.
Nothing in src/ constructs it; track_expenses.py does and passes it down.
"""

import httpx

from src.models import UNCATEGORIZED, CategorizationResult


class OllamaError(Exception):
    pass


def check_ollama_available(host: str, model_name: str) -> None:
    """Fail fast, naming the command that fixes it, before processing anything."""
    url = f"{host.rstrip('/')}/api/tags"
    try:
        response = httpx.get(url, timeout=5.0)
        response.raise_for_status()
    except Exception as exc:
        raise OllamaError(
            f"Cannot reach Ollama at {host}.\n"
            f"  Install it from https://ollama.com, then start it with:  ollama serve\n"
            f"  (original error: {exc})"
        ) from exc

    installed = {model.get("name", "") for model in response.json().get("models", [])}
    installed_bases = {name.split(":")[0] for name in installed}
    if model_name not in installed and model_name.split(":")[0] not in installed_bases:
        raise OllamaError(
            f"Model '{model_name}' is not installed in Ollama at {host}.\n"
            f"  Pull it with:  ollama pull {model_name}\n"
            f"  Installed: {', '.join(sorted(installed)) or '(none)'}"
        )


def build_prompt(description: str, note: str, categories: dict, hints_text: str = "") -> str:
    lines = []
    for category, subcategories in categories.items():
        subs = ", ".join(s for s in (subcategories or []) if s)
        lines.append(f"- {category}: {subs}" if subs else f"- {category}")
    taxonomy = "\n".join(lines)

    prompt = (
        "You categorize personal bank transactions.\n"
        "Choose exactly one category and one of that category's subcategories "
        "from the list below. Also return a short human-readable vendor name "
        "and how confident you are, from 0.0 to 1.0.\n"
        f"If the description does not make the purpose clear, answer "
        f"'{UNCATEGORIZED}' with a low confidence instead of guessing.\n\n"
        f"Categories:\n{taxonomy}\n"
    )
    if hints_text:
        prompt += f"\n{hints_text.rstrip()}\n"
    return prompt + (
        f"\nTransaction description: {description}\n"
        f"Payment note: {note or '(none)'}\n"
    )


def categorize_via_llm(
    description: str,
    note: str,
    categories: dict,
    model,
    hints_text: str = "",
    attempts: int = 2,
) -> CategorizationResult:
    """Categorize one transaction, degrading to 'needs review' rather than
    raising — a malformed response from a small model is worth one retry, not
    an aborted run."""
    prompt = build_prompt(description, note, categories, hints_text)
    for _ in range(attempts):
        try:
            result = model.invoke(prompt)
        except Exception:
            continue
        if result is not None:
            return result
    return CategorizationResult(
        vendor=description, category=UNCATEGORIZED, subcategory="", confidence=0.0
    )
