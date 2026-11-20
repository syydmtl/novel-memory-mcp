"""Small authenticated web API for selecting the active memory scope."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
)
from starlette.routing import Route

from .files import atomic_write, file_lock
from .store import ConflictError, MemoryStore, ScopeError


class PreconditionError(ValueError):
    """A modifying client must first read the current version."""


async def _object_body(request: Any) -> dict[str, Any]:
    body = json.loads(await request.body())
    if not isinstance(body, dict):
        raise TypeError("JSON body must be an object")
    return body


def _string(body: dict[str, Any], key: str, default: str = "") -> str:
    value = body.get(key, default)
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value


def _strings(body: dict[str, Any], key: str) -> list[str]:
    value = body.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TypeError(f"{key} must be a list of strings")
    return value


def _digest(body: dict[str, Any], *, required: bool = False) -> str | None:
    if "expected_digest" not in body:
        if required:
            raise PreconditionError("read the current record and include expected_digest before modifying it")
        return None
    value = _string(body, "expected_digest")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise ValueError("expected_digest must be a SHA-256 digest")
    return value.lower()


def _error(exc: Exception, status: int = 400) -> JSONResponse:
    code = "invalid_request"
    if isinstance(exc, ScopeError):
        status, code = 503, "invalid_scope"
    elif isinstance(exc, ConflictError):
        status, code = 409, "conflict"
    elif isinstance(exc, PreconditionError):
        status, code = 428, "precondition_required"
    elif isinstance(exc, OSError):
        return JSONResponse({"error": "File operation failed; check permissions and available disk space", "code": "storage_error"}, status_code=503)
    result = {"error": str(exc), "code": code}
    if isinstance(exc, ConflictError) and exc.memory_id:
        result["memory_id"] = exc.memory_id
    return JSONResponse(result, status_code=status)


def _matches(presented: str, expected: str) -> bool:
    try:
        return bool(expected) and hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))
    except UnicodeError:
        return False


def _session_value(token: str) -> str:
    return "v1." + hmac.new(token.encode("utf-8"), b"novel-memory-admin-session-v1", hashlib.sha256).hexdigest()


def _set_session(response: JSONResponse, request: Any, token: str) -> None:
    response.set_cookie("novel_memory_session", _session_value(token), httponly=True,
                        secure=request.url.scheme == "https", samesite="lax", max_age=60 * 60 * 24 * 30)


def _admin_token_path() -> Path:
    configured = os.getenv("NOVEL_MEMORY_ADMIN_TOKEN_PATH", "").strip()
    if configured:
        return Path(configured).resolve()
    return store().scope_path.parent / "admin_token.txt"


def _expected_token() -> str:
    path = _admin_token_path()
    if path.is_file():
        try:
            val = path.read_text(encoding="utf-8").strip()
            if val:
                return val
        except OSError:
            pass
    return os.getenv("NOVEL_MEMORY_ADMIN_TOKEN", "").strip()


def _authorized(request: Any) -> bool:
    expected = _expected_token()
    presented = request.headers.get("authorization", "")
    if presented.lower().startswith("bearer "):
        return _matches(presented[7:].strip(), expected)
    cookie = request.cookies.get("novel_memory_session", "")
    # Keep existing ASCII sessions valid; new sessions never contain the password.
    return bool(expected) and (_matches(cookie, _session_value(expected)) or _matches(cookie, expected))


async def guard(request: Any, call_next: Any) -> Any:
    if request.url.path.startswith("/api/") and request.url.path not in {"/api/login"} and not _authorized(request):
        return PlainTextResponse("unauthorized", status_code=401)
    return await call_next(request)


def store() -> MemoryStore:
    return MemoryStore.from_environment()


async def libraries(request: Any) -> JSONResponse:
    if request.method == "POST":
        try:
            body = await _object_body(request)
            result = store().create_library(
                _string(body, "library_id"),
                _string(body, "name"),
                _string(body, "description"),
            )
            return JSONResponse(result, status_code=201)
        except (ValueError, TypeError, OSError) as exc:
            return _error(exc)
    result = []
    for item in store().list_libraries():
        item = dict(item)
        try:
            item["memories"] = store().list_library_memories(str(item["library_id"]), item["errors"])
        except (ValueError, TypeError, OSError, RuntimeError) as exc:
            item["memories"] = []
            item["errors"].append({"file": "memories", "error": str(exc)})
        item["memory_count"] = len(item["memories"])
        result.append(item)
    return JSONResponse(result)


async def library_item(request: Any) -> JSONResponse:
    library_id = request.path_params["library_id"]
    try:
        body = await _object_body(request)
        result = store().update_library(
            library_id,
            _string(body, "name"),
            _string(body, "description"),
        )
        return JSONResponse(result)
    except (ValueError, TypeError, OSError) as exc:
        return _error(exc)


async def memory_collection(request: Any) -> JSONResponse:
    library_id = request.path_params["library_id"]
    try:
        body = await _object_body(request)
        raw_markdown = body.get("raw_markdown")
        overwrite = body.get("overwrite", False)
        if not isinstance(overwrite, bool):
            raise TypeError("overwrite must be a boolean")
        expected_digest = _digest(body, required=overwrite)
        if raw_markdown is not None:
            filename = _string(body, "filename", "memory.md")
            result = store().import_raw_markdown(
                library_id=library_id,
                filename=filename,
                raw_text=_string(body, "raw_markdown"),
                overwrite=overwrite,
                expected_digest=expected_digest,
            )
            return JSONResponse(result, status_code=201)

        result = store().save_library_memory(
            library_id=library_id,
            memory_id=_string(body, "memory_id"),
            date_range=_string(body, "date_range"),
            title=_string(body, "title"),
            tags=body.get("tags", ""),
            content=_string(body, "content"),
            is_new=True,
            overwrite=overwrite,
            expected_digest=expected_digest,
        )
        return JSONResponse(result, status_code=201)
    except (ValueError, TypeError, OSError) as exc:
        return _error(exc)


async def memory_item(request: Any) -> JSONResponse:
    library_id = request.path_params["library_id"]
    memory_id = request.path_params["memory_id"]
    if request.method == "GET":
        try:
            return JSONResponse(store().get_library_memory(library_id, memory_id))
        except (ValueError, TypeError, OSError) as exc:
            return _error(exc, 404)
    elif request.method == "PUT":
        try:
            body = await _object_body(request)
            result = store().save_library_memory(
                library_id=library_id,
                memory_id=memory_id,
                date_range=_string(body, "date_range"),
                title=_string(body, "title"),
                tags=body.get("tags", ""),
                content=_string(body, "content"),
                is_new=False,
                expected_digest=_digest(body, required=True),
            )
            return JSONResponse(result)
        except (ValueError, TypeError, OSError) as exc:
            return _error(exc)
    elif request.method == "DELETE":
        try:
            body = await _object_body(request)
            store().delete_library_memory(library_id, memory_id, _digest(body, required=True))
            return JSONResponse({"ok": True})
        except (ValueError, TypeError, OSError) as exc:
            return _error(exc)
    return JSONResponse({"error": "method not allowed"}, status_code=405)


async def scope(request: Any) -> JSONResponse:
    if request.method == "GET":
        try:
            return JSONResponse(store().active_scope())
        except ScopeError as exc:
            return _error(exc)
    try:
        body = await _object_body(request)
        result = store().set_scope(_string(body, "library_id"), _strings(body, "memory_ids"))
        return JSONResponse(result)
    except (ValueError, TypeError, OSError) as exc:
        return _error(exc)


async def token_management(request: Any) -> JSONResponse:
    if request.method == "POST":
        try:
            body = await _object_body(request)
            new_token = _string(body, "new_token").strip()
            if len(new_token) < 6:
                return JSONResponse({"error": "新 Token 密码长度不得少于 6 位"}, status_code=400)
            if len(new_token) > 256 or any(ord(character) < 32 for character in new_token):
                raise ValueError("password must be at most 256 characters and contain no control characters")
            response = JSONResponse({"ok": True})
            _set_session(response, request, new_token)
            token_path = _admin_token_path()
            with file_lock(token_path, store().index_path.parent / "locks"):
                atomic_write(token_path, new_token + "\n")
            return response
        except (ValueError, TypeError, OSError) as exc:
            return _error(exc)
    return JSONResponse({"error": "method not allowed"}, status_code=405)


async def login(request: Any) -> JSONResponse:
    try:
        body = await _object_body(request)
        token = _string(body, "token").strip()
    except (ValueError, TypeError) as exc:
        return _error(exc)
    expected = _expected_token()
    if not _matches(token, expected):
        return JSONResponse({"error": "管理密码不正确"}, status_code=401)
    response = JSONResponse({"ok": True})
    _set_session(response, request, token)
    return response


async def logout(request: Any) -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie("novel_memory_session")
    return response


LOGIN_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Novel Memory 登录</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f5f6f8;color:#18181b;font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}.box{width:min(390px,calc(100% - 32px));background:#fff;border:1px solid #e4e4e7;border-radius:12px;padding:30px;box-shadow:0 12px 35px #18181b12}.mark{font-size:22px;font-weight:750}.muted{color:#71717a;font-size:13px;margin:8px 0 26px}.label{font-size:13px;font-weight:650;display:block;margin-bottom:8px}.input{width:100%;box-sizing:border-box;border:1px solid #d4d4d8;border-radius:7px;padding:11px 12px;font:inherit;outline:none}.input:focus{border-color:#2563eb;box-shadow:0 0 0 3px #2563eb1c}.button{width:100%;margin-top:16px;border:0;border-radius:7px;padding:11px;background:#2563eb;color:#fff;font:650 14px inherit;cursor:pointer}.button:hover{background:#1d4ed8}.error{color:#b91c1c;font-size:13px;margin-top:12px;min-height:19px}</style></head>
<body><main class="box"><div class="mark">Novel Memory</div><p class="muted">小说历史记忆工作台<br>请输入管理密码继续</p><form id="form"><label class="label" for="token">管理密码</label><input class="input" id="token" type="password" autocomplete="current-password" autofocus required><button class="button" type="submit">登录工作台</button><div class="error" id="error"></div></form></main><script>form.onsubmit=async e=>{e.preventDefault();error.textContent='';const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:token.value})});if(r.ok){location.reload()}else{error.textContent='管理密码不正确，请重试'}};</script></body></html>"""


def create_app() -> Starlette:
    web_dir = Path(os.getenv("NOVEL_MEMORY_WEB_DIR", "web"))

    async def index(request: Any) -> Any:
        if not _authorized(request):
            return HTMLResponse(LOGIN_HTML)
        return FileResponse(web_dir / "index.html")

    async def assets(request: Any) -> Any:
        path = web_dir / request.path_params["path"]
        if not path.is_file() or web_dir.resolve() not in path.resolve().parents:
            return PlainTextResponse("not found", status_code=404)
        return FileResponse(path)

    app = Starlette(routes=[
        Route("/", index),
        Route("/api/libraries", libraries, methods=["GET", "POST"]),
        Route("/api/libraries/{library_id}", library_item, methods=["PUT"]),
        Route("/api/libraries/{library_id}/memories", memory_collection, methods=["POST"]),
        Route("/api/libraries/{library_id}/memories/{memory_id}", memory_item, methods=["GET", "PUT", "DELETE"]),
        Route("/api/scope", scope, methods=["GET", "POST"]),
        Route("/api/login", login, methods=["POST"]),
        Route("/api/logout", logout, methods=["POST"]),
        Route("/api/token", token_management, methods=["POST"]),
        Route("/{path:path}", assets),
    ])
    class AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Any, call_next: Any) -> Any:
            return await guard(request, call_next)

    app.add_middleware(AuthMiddleware)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.getenv("NOVEL_MEMORY_ADMIN_HOST", "127.0.0.1"), port=int(os.getenv("NOVEL_MEMORY_ADMIN_PORT", "28767")))
