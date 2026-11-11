import os
import re
from dataclasses import dataclass
from pathlib import Path


def _pos_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}") from None
    if v <= 0:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}")
    return v


_BOOLS = {"true": True, "1": True, "yes": True, "false": False, "0": False, "no": False}


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return _BOOLS[raw.strip().lower()]
    except KeyError:
        raise ValueError(f"{name} must be one of true/false/1/0/yes/no, got {raw!r}") from None


def _hhmm(name: str, default: str) -> str:
    raw = os.environ.get(name)
    if raw is None:
        return default
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", raw):
        raise ValueError(f"{name} must be HH:MM (00:00-23:59), got {raw!r}")
    return raw


def _backup_dir() -> Path | None:
    raw = os.environ.get("BACKUP_DIR")
    if raw is None:
        return None
    if not raw.strip():
        raise ValueError("BACKUP_DIR must not be empty")
    return Path(raw)


@dataclass(frozen=True)
class Settings:
    server_url: str
    data_dir: Path
    poll_seconds: int = 30
    port: int = 8081
    backup_enabled: bool = True
    backup_time: str = "03:00"  # HH:MM Europe/Dublin local time
    backup_keep: int = 7
    backup_dir: Path | None = None  # None -> <data_dir>/backups; a relative path is kept as given (cwd-relative)
    server_timeout_seconds: int = 45  # read timeout for calls to the server (connect stays 5 s)
    server_concurrency: int = 2  # routes fetched at once

    @property
    def backup_path(self) -> Path:
        return self.backup_dir if self.backup_dir is not None else self.data_dir / "backups"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            server_url=os.environ.get("SERVER_URL", "http://dublin-bus-server:8080"),
            data_dir=Path(os.environ.get("DATA_DIR", "/data")),
            poll_seconds=_pos_int("POLL_SECONDS", 30),
            port=_pos_int("PORT", 8081),
            backup_enabled=_bool("BACKUP_ENABLED", True),
            backup_time=_hhmm("BACKUP_TIME", "03:00"),
            backup_keep=_pos_int("BACKUP_KEEP", 7),
            backup_dir=_backup_dir(),
            server_timeout_seconds=_pos_int("SERVER_TIMEOUT_SECONDS", 45),
            server_concurrency=_pos_int("SERVER_CONCURRENCY", 2),
        )
