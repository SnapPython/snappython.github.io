from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_path: Path
    static_dir: Path
    proxy_secret: str
    allowed_users: frozenset[str]
    base_url: str
    timezone: str
    google_client_id: str
    google_client_secret: str
    google_token_key: str
    sync_interval_seconds: int

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(
            os.getenv("LC_DATA_DIR", "/var/lib/life-cockpit")
        ).expanduser()
        database_path = Path(
            os.getenv("LC_DATABASE_PATH", str(data_dir / "life-cockpit.db"))
        ).expanduser()
        static_dir = Path(
            os.getenv("LC_STATIC_DIR", "/var/www/life-cockpit")
        ).expanduser()
        allowed_users = frozenset(
            item.strip().lower()
            for item in os.getenv("LC_ALLOWED_USERS", "").split(",")
            if item.strip()
        )
        return cls(
            data_dir=data_dir,
            database_path=database_path,
            static_dir=static_dir,
            proxy_secret=os.getenv("LC_PROXY_SECRET", ""),
            allowed_users=allowed_users,
            base_url=os.getenv(
                "LC_BASE_URL",
                "https://life.72-11-130-223.sslip.io:8444",
            ).rstrip("/"),
            timezone=os.getenv("LC_TIMEZONE", "Asia/Shanghai"),
            google_client_id=os.getenv("GOOGLE_CLIENT_ID", ""),
            google_client_secret=os.getenv("GOOGLE_CLIENT_SECRET", ""),
            google_token_key=os.getenv("LC_GOOGLE_TOKEN_KEY", ""),
            sync_interval_seconds=max(
                30, int(os.getenv("LC_SYNC_INTERVAL_SECONDS", "60"))
            ),
        )

    @property
    def google_configured(self) -> bool:
        return bool(
            self.google_client_id
            and self.google_client_secret
            and self.google_token_key
        )

    @property
    def google_redirect_uri(self) -> str:
        return f"{self.base_url}/api/google/callback"
