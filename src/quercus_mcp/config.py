"""Paths, user configuration and access-token storage."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, asdict
from pathlib import Path
from urllib.parse import urlparse

KEYRING_SERVICE = "quercus-mcp"
DEFAULT_BASE_URL = "https://q.utoronto.ca"


@dataclass(frozen=True)
class Paths:
    home: Path

    @classmethod
    def default(cls) -> "Paths":
        env = os.environ.get("QUERCUS_HOME")
        return cls(Path(env).expanduser() if env else Path.home() / ".quercus-mcp")

    @property
    def db(self) -> Path:
        return self.home / "quercus.db"

    @property
    def files_dir(self) -> Path:
        return self.home / "files"

    @property
    def text_dir(self) -> Path:
        return self.home / "text"

    @property
    def config_path(self) -> Path:
        return self.home / "config.toml"

    @property
    def log_path(self) -> Path:
        return self.home / "quercus.log"

    def ensure(self) -> None:
        for d in (self.home, self.files_dir, self.text_dir):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)


@dataclass
class Config:
    base_url: str = DEFAULT_BASE_URL
    sync_interval_minutes: int = 30
    volatile_ttl_minutes: int = 10
    max_file_mb: int = 50
    include_courses: list[int] = field(default_factory=list)
    exclude_courses: list[int] = field(default_factory=list)
    all_terms: bool = False

    def __post_init__(self) -> None:
        self.base_url = self.base_url.rstrip("/")

    @property
    def host(self) -> str:
        return urlparse(self.base_url).netloc

    @classmethod
    def load(cls, paths: Paths) -> "Config":
        if not paths.config_path.exists():
            return cls()
        data = tomllib.loads(paths.config_path.read_text())
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self, paths: Paths) -> None:
        paths.ensure()
        lines = []
        for k, v in asdict(self).items():
            lines.append(f"{k} = {_toml_value(v)}")
        paths.config_path.write_text("\n".join(lines) + "\n")
        paths.config_path.chmod(0o600)


def _toml_value(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    raise TypeError(f"unsupported config value: {v!r}")


# --- token storage -------------------------------------------------------

def _keyring_get(host: str) -> str | None:
    try:
        import keyring
        return keyring.get_password(KEYRING_SERVICE, host)
    except Exception:
        return None


def _keyring_set(host: str, token: str) -> None:
    import keyring
    keyring.set_password(KEYRING_SERVICE, host, token)


def _keyring_delete(host: str) -> None:
    try:
        import keyring
        keyring.delete_password(KEYRING_SERVICE, host)
    except Exception:
        pass


def get_token(host: str) -> str | None:
    env = os.environ.get("QUERCUS_TOKEN")
    if env:
        return env.strip()
    return _keyring_get(host)


def set_token(host: str, token: str) -> None:
    _keyring_set(host, token.strip())


def delete_token(host: str) -> None:
    _keyring_delete(host)
