"""Smoke test using this copy's local .env configuration."""

from run import check, configure_console, load_settings

if __name__ == "__main__":
    configure_console()
    load_settings()
    check()
