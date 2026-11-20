"""Portable launcher; configuration is local to this copy of the project."""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PATH_DEFAULTS = {
    "NOVEL_MEMORY_DIR": "memories",
    "NOVEL_MEMORY_LIBRARIES_DIR": "libraries",
    "NOVEL_MEMORY_INDEX_PATH": "state/novel_memory_index.sqlite3",
    "NOVEL_MEMORY_SCOPE_PATH": "state/active_scope.json",
    "NOVEL_MEMORY_WEB_DIR": "web",
    "NOVEL_MEMORY_ADMIN_TOKEN_PATH": "state/admin_token.txt",  # nosec B105 - File path.
}
VALUE_DEFAULTS = {
    "NOVEL_MEMORY_RUN_MODE": "http",
    "NOVEL_MEMORY_HOST": "127.0.0.1",
    "NOVEL_MEMORY_PORT": "8766",
    "NOVEL_MEMORY_TOKEN": "",  # nosec B105 - Setup generates a random credential.
    "NOVEL_MEMORY_ALLOWED_HOSTS": "127.0.0.1:8766,localhost:8766",
    "NOVEL_MEMORY_ALLOWED_ORIGINS": "http://127.0.0.1:8766,http://localhost:8766",
    "NOVEL_MEMORY_ADMIN_HOST": "127.0.0.1",
    "NOVEL_MEMORY_ADMIN_PORT": "8767",
    "NOVEL_MEMORY_ADMIN_TOKEN": "",  # nosec B105 - Setup generates a random credential.
}


def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")


def load_settings() -> None:
    """Load plain key=value text, without executing shell code or inheriting paths."""
    config_path = ROOT / ".env"
    if not config_path.is_file():
        raise SystemExit("Run setup first: python run.py setup")
    values: dict[str, str] = {}
    supported = set(PATH_DEFAULTS) | set(VALUE_DEFAULTS)
    for number, raw in enumerate(config_path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or key not in supported or key in values:
            raise SystemExit(f"Invalid or duplicate setting on .env line {number}")
        values[key] = value
    os.chdir(ROOT)
    for key, default in VALUE_DEFAULTS.items():
        os.environ[key] = values.get(key, default)
    for key, default in PATH_DEFAULTS.items():
        path = Path(values.get(key, default))
        if not path.is_absolute():
            path = ROOT / path
        os.environ[key] = str(path.resolve())


def setup() -> None:
    """Generate independent credentials; never overwrite an existing configuration."""
    config_path = ROOT / ".env"
    if config_path.exists():
        raise SystemExit(".env already exists; setup will not overwrite it.")
    from novel_memory_mcp.store import MemoryStore

    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    text = text.replace("GENERATE_MCP_TOKEN_WITH_SETUP", secrets.token_urlsafe(32))
    text = text.replace("GENERATE_ADMIN_TOKEN_WITH_SETUP", secrets.token_urlsafe(32))
    with config_path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    config_path.chmod(0o600)
    load_settings()
    store = MemoryStore.from_environment()
    if not store.scope_path.exists():
        store.set_scope("demo_story", ["demo_stage_01", "demo_stage_02"])
    print("Setup complete. Independent MCP and admin credentials are in .env.")
    print("Use NOVEL_MEMORY_ADMIN_TOKEN to log in to the management page.")
    print("Next: python run.py admin  |  python run.py mcp")


def check() -> None:
    from novel_memory_mcp.store import MemoryStore

    store = MemoryStore.from_environment()
    tags = store.list_tags()
    print(f"Index OK; {len(tags)} stage(s) are currently available.")
    if tags:
        memory_id = str(tags[0]["memory_id"])
        block = store.get(memory_id)
        if not block["content"]:
            raise SystemExit("Read check failed: empty memory content")
        print(f"Read OK: {memory_id}")


def main() -> None:
    configure_console()
    parser = argparse.ArgumentParser(description="Novel Memory MCP share edition")
    parser.add_argument("command", choices=("setup", "admin", "mcp", "stdio", "check"))
    args = parser.parse_args()
    if args.command == "setup":
        setup()
        return
    load_settings()
    if args.command == "check":
        check()
    elif args.command == "admin":
        import uvicorn

        from novel_memory_mcp.admin import create_app

        if not os.environ["NOVEL_MEMORY_ADMIN_TOKEN"]:
            raise SystemExit("Set NOVEL_MEMORY_ADMIN_TOKEN in .env first.")
        uvicorn.run(
            create_app(),
            host=os.environ["NOVEL_MEMORY_ADMIN_HOST"],
            port=int(os.environ["NOVEL_MEMORY_ADMIN_PORT"]),
        )
    else:
        os.environ["NOVEL_MEMORY_RUN_MODE"] = "stdio" if args.command == "stdio" else "http"
        from novel_memory_mcp.server import main as server_main

        server_main()


if __name__ == "__main__":
    main()
