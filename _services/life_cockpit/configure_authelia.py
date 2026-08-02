from __future__ import annotations

from pathlib import Path


CONFIG_PATH = Path("/opt/sso/authelia/configuration.yml")
EXISTING_DOMAIN = "        - 'mechmind.72-11-130-223.sslip.io'\n"
LIFE_DOMAIN = "        - 'life.72-11-130-223.sslip.io'\n"


def main() -> None:
    content = CONFIG_PATH.read_text(encoding="utf-8")
    if LIFE_DOMAIN in content:
        print("Authelia already allows the life cockpit domain")
        return
    if EXISTING_DOMAIN not in content:
        raise RuntimeError("Expected Authelia two-factor rule was not found")
    CONFIG_PATH.write_text(
        content.replace(
            EXISTING_DOMAIN,
            f"{EXISTING_DOMAIN}{LIFE_DOMAIN}",
            1,
        ),
        encoding="utf-8",
    )
    print("Added the life cockpit domain to Authelia")


if __name__ == "__main__":
    main()
