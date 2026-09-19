from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OVERRIDE_DIR = ROOT / ".cache" / "prompts"
DEFAULT_PROMPT_FILE = ROOT / "mark_pipeline_test" / "prompt.txt"

PROMPT_REGISTRY: dict[str, dict[str, str]] = {
    "analyze": {
        "title": "Анализ расстояний по вершинам",
        "default_file": str(DEFAULT_PROMPT_FILE),
    },
}


def _default_text(name: str) -> str:
    spec = PROMPT_REGISTRY.get(name)
    if not spec:
        return ""
    text = Path(spec["default_file"]).read_text(encoding="utf-8", errors="ignore")
    match = re.search(r'PROMPT_TEMPLATE = """([\s\S]*?)"""', text)
    return match.group(1) if match else text


def _override_path(name: str) -> Path:
    return OVERRIDE_DIR / f"{name}.txt"


def list_prompts() -> list[dict[str, str]]:
    output = []
    for name, spec in PROMPT_REGISTRY.items():
        output.append(
            {
                "name": name,
                "title": spec.get("title", name),
                "source": "override" if _override_path(name).exists() else "default",
            }
        )
    return output


def load_prompt(name: str) -> str:
    override = _override_path(name)
    if override.exists():
        return override.read_text(encoding="utf-8")
    return _default_text(name)


def save_prompt(name: str, text: str) -> None:
    if name not in PROMPT_REGISTRY:
        raise KeyError(f"Неизвестный промпт: {name}")
    OVERRIDE_DIR.mkdir(parents=True, exist_ok=True)
    _override_path(name).write_text(text, encoding="utf-8")


def reset_prompt(name: str) -> None:
    override = _override_path(name)
    if override.exists():
        override.unlink()
