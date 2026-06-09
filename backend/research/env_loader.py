"""Load API keys for live research runs without exposing them in the shell.

The repo has no local ``.env``; keys live in ``env.example``. This loads them
in-process via python-dotenv (already a dependency) so live CLI runs work
without exporting secrets into shell history or logs. Values are never printed.
"""
from __future__ import annotations

import os

_KEYS = (
    "ORATS_TOKEN",
    "EODHD_API_TOKEN",
    "API_NINJAS_API_KEY",
    "OPENAI_API_KEY",
    "LLM_MODEL_NARRATIVE",
)


def load_research_env(path: str | None = None) -> list[str]:
    """Populate missing env vars from a dotenv file. Returns names loaded."""
    from dotenv import dotenv_values

    if path is None:
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        for candidate in (".env", "env.example"):
            p = os.path.join(repo_root, candidate)
            if os.path.exists(p):
                path = p
                break
    if not path or not os.path.exists(path):
        return []

    vals = dotenv_values(path)
    loaded: list[str] = []
    for key in _KEYS:
        if os.getenv(key):
            continue
        v = (vals.get(key) or "").strip()
        if v:
            os.environ[key] = v
            loaded.append(key)
    return loaded
