from __future__ import annotations

import json
import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OVERRIDE_DIR = ROOT / ".cache" / "prompts"
PROMPT_REGISTRY: dict[str, dict[str, str]] = {
    "dimension_review": {
        "title": "Проверка кандидатов размеров и объектов вне трубы",
        "default_file": str(ROOT / "prompts" / "defaults" / "dimension_review.txt"),
        "usage": "Этап «Карта размеров + проверка провайдером»",
    },
    "pipeline_length": {
        "title": "Расчет длины трубопровода",
        "default_file": str(ROOT / "prompts" / "defaults" / "pipeline_length.txt"),
        "usage": "Этап «Расчет длины трубопровода»",
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


def _versions_path(name: str) -> Path:
    return OVERRIDE_DIR / f"{name}.versions.jsonl"


def _selection_path(name: str) -> Path:
    return OVERRIDE_DIR / f"{name}.active.json"


def _remember_selection(name: str, version: int, text: str) -> None:
    _selection_path(name).write_text(json.dumps({
        "version": version,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }), encoding="utf-8")


def list_prompts() -> list[dict[str, str]]:
    output = []
    for name, spec in PROMPT_REGISTRY.items():
        output.append(
            {
                "name": name,
                "title": spec.get("title", name),
                "usage": spec.get("usage", ""),
                "source": "override" if _override_path(name).exists() else "default",
            }
        )
    return output


def load_prompt(name: str) -> str:
    override = _override_path(name)
    if override.exists():
        return override.read_text(encoding="utf-8")
    return _default_text(name)


def load_prompt_revision(name: str) -> dict[str, object]:
    """Return the active prompt together with stable metadata for a run."""
    text = load_prompt(name)
    versions = list_versions(name)
    override = _override_path(name).exists()
    sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    matching = next((item for item in reversed(versions) if item.get("sha256") == sha256), None)
    # Identical texts can belong to different saved versions. Preserve the choice.
    if override and _selection_path(name).exists():
        try:
            selected = json.loads(_selection_path(name).read_text(encoding="utf-8"))
        except (ValueError, OSError):
            selected = {}
        if isinstance(selected, dict) and selected.get("sha256") == sha256:
            matching = next((item for item in versions
                             if item["version"] == selected.get("version")
                             and item["sha256"] == sha256), matching)
    return {
        "name": name,
        "text": text,
        "source": "override" if override else "default",
        "version": matching["version"] if matching is not None else ("unversioned" if override else "default"),
        "sha256": sha256,
    }


def save_prompt(name: str, text: str) -> None:
    if name not in PROMPT_REGISTRY:
        raise KeyError(f"Неизвестный промпт: {name}")
    if load_prompt(name) == text:
        return
    OVERRIDE_DIR.mkdir(parents=True, exist_ok=True)
    _override_path(name).write_text(text, encoding="utf-8")
    entry = {
        "version": len(list_versions(name)),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "text": text,
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    with _versions_path(name).open("a", encoding="utf-8") as file:
        file.write(json.dumps(entry, ensure_ascii=False) + "\n")
    _remember_selection(name, entry["version"], text)


def list_versions(name: str) -> list[dict[str, object]]:
    path = _versions_path(name)
    if not path.exists():
        return []
    versions = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        text = entry.get("text", "")
        if not isinstance(text, str):
            continue
        versions.append(
            {
                "version": entry.get("version", len(versions)),
                "created_at": entry.get("created_at", ""),
                "preview": text[:80],
                "length": len(text),
                "sha256": entry.get("sha256") or hashlib.sha256(text.encode("utf-8")).hexdigest(),
            }
        )
    return versions


def get_version_text(name: str, version: int) -> str:
    path = _versions_path(name)
    if not path.exists():
        raise IndexError("Версий нет")
    entries = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("text"), str):
            entries.append(entry)
    if not (0 <= version < len(entries)):
        raise IndexError("Неверный номер версии")
    return entries[version]["text"]


def restore_version(name: str, version: int) -> None:
    text = get_version_text(name, version)
    OVERRIDE_DIR.mkdir(parents=True, exist_ok=True)
    _override_path(name).write_text(text, encoding="utf-8")
    _remember_selection(name, version, text)


def resolve_recorded_revision(
    name: str, version: object, sha256: str | None, text: str | None,
) -> dict[str, object]:
    """Recover missing metadata only when the stored evidence is unambiguous."""
    if version is not None and version != "" and sha256:
        return {"name": name, "version": str(version), "sha256": sha256}
    digest = sha256 or (hashlib.sha256(text.encode("utf-8")).hexdigest() if text else None)
    matches = [item for item in list_versions(name) if item["sha256"] == digest]
    if version is not None and version != "":
        matches = [item for item in matches if str(item["version"]) == str(version)]
    if len(matches) == 1:
        version = str(matches[0]["version"])
    elif version in (None, "") and not matches and name in PROMPT_REGISTRY:
        default_hash = hashlib.sha256(_default_text(name).encode("utf-8")).hexdigest()
        version = "default" if digest == default_hash else None
    return {"name": name, "version": version, "sha256": digest}


def reset_prompt(name: str) -> None:
    override = _override_path(name)
    if override.exists():
        override.unlink()
