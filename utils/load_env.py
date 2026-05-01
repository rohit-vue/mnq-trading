"""Load `.env` from project root (optional dependency: python-dotenv)."""

_loaded = False


def load_project_dotenv() -> None:
    """Call once at process start so OS env overrides YAML for secrets."""
    global _loaded
    if _loaded:
        return
    try:
        from pathlib import Path

        from dotenv import load_dotenv

        root = Path(__file__).resolve().parent.parent
        load_dotenv(root / ".env")
    except ImportError:
        pass
    _loaded = True
