import os
from pathlib import Path


def env_path() -> Path:
    return Path(os.getenv("ENV_FILE", ".env"))


def env_encode(value: str) -> str:
    if value == "":
        return ""
    safe_chars = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./:-[]")
    if all(char in safe_chars for char in value):
        return value
    # Use single quotes so dotenv treats the content literally (no escape processing).
    # If the value itself contains single quotes, fall back to double-quote with escaping.
    if "'" not in value:
        return f"'{value}'"
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_env_updates(updates: dict[str, str]) -> None:
    path = env_path()
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    remaining = dict(updates)
    next_lines: list[str] = []
    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            next_lines.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            next_lines.append(f"{key}={env_encode(remaining.pop(key))}")
        else:
            next_lines.append(line)
    if remaining and next_lines and next_lines[-1].strip():
        next_lines.append("")
    for key in sorted(remaining):
        next_lines.append(f"{key}={env_encode(remaining[key])}")
    path.write_text("\n".join(next_lines).rstrip() + "\n", encoding="utf-8")
