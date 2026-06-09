from pathlib import Path


def template_path(name: str) -> Path:
    return Path(__file__).parent / "templates" / name


def read_template(name: str) -> str:
    return template_path(name).read_text(encoding="utf-8")
