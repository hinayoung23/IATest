from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, StrictUndefined


PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"
ENV = Environment(undefined=StrictUndefined, autoescape=False)


def render_prompt(name: str, context: dict[str, str] | None = None) -> str:
    content = (PROMPT_DIR / name).read_text(encoding="utf-8")
    template = ENV.from_string(content)
    return template.render(**(context or {})).strip()
