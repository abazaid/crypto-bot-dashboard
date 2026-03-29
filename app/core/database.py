from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.config import settings
from pathlib import Path


def _normalize_sqlite_url(url: str) -> str:
    # Keep non-sqlite URLs unchanged.
    if not url.startswith("sqlite:///"):
        return url
    raw_path = url.replace("sqlite:///", "", 1)
    # Keep explicit absolute sqlite paths unchanged.
    if Path(raw_path).is_absolute():
        return url
    # Resolve relative sqlite path against project root (stable across cwd changes).
    project_root = Path(__file__).resolve().parents[2]
    abs_path = (project_root / raw_path).resolve()
    return f"sqlite:///{abs_path.as_posix()}"


database_url = _normalize_sqlite_url(settings.database_url)

engine = create_engine(
    database_url,
    connect_args={"check_same_thread": False, "timeout": 30} if database_url.startswith("sqlite") else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
