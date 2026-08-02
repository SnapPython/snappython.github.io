from __future__ import annotations

from pathlib import Path

import httpx


ENV_PATH = Path("/opt/life-cockpit/.env")
API_URL = "http://127.0.0.1:9550/api/status"


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def main() -> None:
    env = read_env()
    headers = {
        "Remote-User": env["LC_ALLOWED_USERS"].split(",", 1)[0].strip(),
        "X-LC-Proxy-Secret": env["LC_PROXY_SECRET"],
    }
    response = httpx.get(API_URL, headers=headers, timeout=10)
    response.raise_for_status()
    payload = response.json()
    print(
        "life-cockpit ok "
        f"revision={payload['revision']} "
        f"has_data={payload['hasData']} "
        f"google_configured={payload['google']['configured']}"
    )


if __name__ == "__main__":
    main()
