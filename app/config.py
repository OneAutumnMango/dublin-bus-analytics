import os
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


@dataclass(frozen=True)
class Settings:
    server_url: str
    data_dir: Path
    poll_seconds: int = 30
    port: int = 8081

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            server_url=os.environ.get("SERVER_URL", "http://dublin-bus-server:8080"),
            data_dir=Path(os.environ.get("DATA_DIR", "/data")),
            poll_seconds=_pos_int("POLL_SECONDS", 30),
            port=_pos_int("PORT", 8081),
        )
