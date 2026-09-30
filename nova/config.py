"""Central settings. Everything tunable comes from .env so reviewers can switch
models / modes without touching code."""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _env(name: str, default: str) -> str:
    return os.getenv(name, default) or default


@dataclass(frozen=True)
class Settings:
    mode: str = _env("NOVA_MODE", "offline").lower()
    extractor_model: str = _env("EXTRACTOR_MODEL", "gemini/gemini-2.5-flash")
    fallback_model: str = os.getenv("FALLBACK_MODEL", "")
    text_model: str = _env("TEXT_MODEL", "gemini/gemini-2.5-flash")

    budget_usd_per_doc: float = float(_env("BUDGET_USD_PER_DOC", "0.05"))
    llm_timeout_s: int = int(_env("LLM_TIMEOUT_S", "60"))
    llm_max_retries: int = int(_env("LLM_MAX_RETRIES", "2"))
    max_pages: int = int(_env("MAX_PAGES", "5"))
    render_dpi: int = 150      # first pass
    refine_dpi: int = 220      # fallback re-read of low-confidence fields
    recursion_limit: int = 12  # hard cap on graph steps (graph is a DAG; this is a backstop)

    rules_dir: Path = ROOT / "rules"
    db_path: Path = Path(_env("NOVA_DB_PATH", str(ROOT / "data" / "nova.db")))
    checkpoint_path: Path = Path(_env("NOVA_CHECKPOINT_PATH", str(ROOT / "data" / "checkpoints.db")))
    runs_dir: Path = ROOT / "data" / "runs"
    samples_dir: Path = ROOT / "data" / "samples"

    langfuse_enabled: bool = field(default_factory=lambda: bool(os.getenv("LANGFUSE_PUBLIC_KEY")))


SETTINGS = Settings()
for p in (SETTINGS.db_path.parent, SETTINGS.checkpoint_path.parent, SETTINGS.runs_dir):
    p.mkdir(parents=True, exist_ok=True)
