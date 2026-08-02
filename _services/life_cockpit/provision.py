from __future__ import annotations

import os
import secrets
from pathlib import Path

from cryptography.fernet import Fernet


ENV_PATH = Path("/opt/life-cockpit/.env")


def main() -> None:
    if ENV_PATH.exists():
        print(f"{ENV_PATH} already exists; leaving it unchanged")
        return

    content = "\n".join(
        [
            "LC_DATA_DIR=/var/lib/life-cockpit",
            "LC_DATABASE_PATH=/var/lib/life-cockpit/life-cockpit.db",
            "LC_STATIC_DIR=/var/www/life-cockpit",
            f"LC_PROXY_SECRET={secrets.token_hex(32)}",
            "LC_ALLOWED_USERS=admin",
            "LC_BASE_URL=https://life.72-11-130-223.sslip.io:8444",
            "LC_TIMEZONE=Asia/Shanghai",
            "LC_SYNC_INTERVAL_SECONDS=60",
            "GOOGLE_CLIENT_ID=",
            "GOOGLE_CLIENT_SECRET=",
            f"LC_GOOGLE_TOKEN_KEY={Fernet.generate_key().decode()}",
            "",
        ]
    )
    ENV_PATH.write_text(content, encoding="utf-8")
    os.chmod(ENV_PATH, 0o600)
    print(f"created {ENV_PATH}")


if __name__ == "__main__":
    main()
