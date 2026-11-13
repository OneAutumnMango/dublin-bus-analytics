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


def _archive_dir() -> tuple[Path | None, bool]:
    raw = os.environ.get("BACKUP_ARCHIVE_DIR")
    if raw is None:
        return None, True
    return (Path(raw), True) if raw.strip() else (None, False)  # blank disables the archive copy


def _hosts() -> tuple[str, ...]:
    return tuple(h for h in (p.strip() for p in os.environ.get("ALLOWED_HOSTS", "").split(",")) if h)


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
    backup_archive_dir: Path | None = None  # None -> <backup_path>/archive
    backup_archive_enabled: bool = True
    server_timeout_seconds: int = 45  # read timeout for calls to the server (connect stays 5 s)
    server_concurrency: int = 2  # routes fetched at once
    allowed_hosts: tuple[str, ...] = ()  # empty -> no Host check

    @property
    def backup_path(self) -> Path:
        return self.backup_dir if self.backup_dir is not None else self.data_dir / "backups"

    @property
    def archive_path(self) -> Path | None:
        if not self.backup_archive_enabled:
            return None
        return self.backup_archive_dir or self.backup_path / "archive"

    @classmethod
    def from_env(cls) -> "Settings":
        archive_dir, archive_on = _archive_dir()
        return cls(
            server_url=os.environ.get("SERVER_URL", "http://dublin-bus-server:8080"),
            data_dir=Path(os.environ.get("DATA_DIR", "/data")),
            poll_seconds=_pos_int("POLL_SECONDS", 30),
            port=_pos_int("PORT", 8081),
            backup_enabled=_bool("BACKUP_ENABLED", True),
            backup_time=_hhmm("BACKUP_TIME", "03:00"),
            backup_keep=_pos_int("BACKUP_KEEP", 7),
            backup_dir=_backup_dir(),
            backup_archive_dir=archive_dir,
            backup_archive_enabled=archive_on,
            server_timeout_seconds=_pos_int("SERVER_TIMEOUT_SECONDS", 45),
            server_concurrency=_pos_int("SERVER_CONCURRENCY", 2),
            allowed_hosts=_hosts(),
        )
