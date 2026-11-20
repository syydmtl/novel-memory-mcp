from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from novel_memory_mcp import admin
from novel_memory_mcp.store import MemoryStore


def setup_test_store(tmp_path: Path) -> MemoryStore:
    libraries_dir = tmp_path / "libraries"
    lib_dir = libraries_dir / "test_lib"
    mem_dir = lib_dir / "memories"
    mem_dir.mkdir(parents=True)
    (lib_dir / "library.json").write_text(
        json.dumps({"library_id": "test_lib", "name": "测试世界观", "description": "测试描述"}, ensure_ascii=False),
        encoding="utf-8",
    )
    (mem_dir / "memory_01.md").write_text(
        "---\nid: memory_01\ndate_range: 100年\ntitle: 初始阶段\ntags: 序章, 角色A\n---\n\n这是第一阶段的历史正文内容。\n",
        encoding="utf-8",
    )
    store = MemoryStore(mem_dir, tmp_path / "state" / "index.sqlite3")
    return store


def test_get_library_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = setup_test_store(tmp_path)
    monkeypatch.setenv("NOVEL_MEMORY_LIBRARIES_DIR", str(tmp_path / "libraries"))
    data = store.get_library_memory("test_lib", "memory_01")
    assert data["memory_id"] == "memory_01"
    assert data["title"] == "初始阶段"
    assert data["content"] == "这是第一阶段的历史正文内容。"
    assert "序章" in data["tags"]


def test_save_and_update_library_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = setup_test_store(tmp_path)
    monkeypatch.setenv("NOVEL_MEMORY_LIBRARIES_DIR", str(tmp_path / "libraries"))

    # Create new
    created = store.save_library_memory(
        library_id="test_lib",
        memory_id="memory_02",
        date_range="101年",
        title="第二阶段",
        tags=["决战", "角色B"],
        content="第二阶段的激战正文。",
        is_new=True,
    )
    assert created["memory_id"] == "memory_02"
    assert created["content"] == "第二阶段的激战正文。"

    # Duplicate create should raise
    with pytest.raises(ValueError, match="already exists"):
        store.save_library_memory(
            library_id="test_lib",
            memory_id="memory_02",
            date_range="101年",
            title="重复阶段",
            tags="tag",
            content="内容",
            is_new=True,
        )

    # Update existing
    updated = store.save_library_memory(
        library_id="test_lib",
        memory_id="memory_02",
        date_range="101年—102年",
        title="第二阶段修订",
        tags=["决战", "角色B", "补遗"],
        content="修改后的第二阶段正文。",
        is_new=False,
    )
    assert updated["title"] == "第二阶段修订"
    assert updated["content"] == "修改后的第二阶段正文。"


def test_delete_library_memory_cleans_scope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = setup_test_store(tmp_path)
    monkeypatch.setenv("NOVEL_MEMORY_LIBRARIES_DIR", str(tmp_path / "libraries"))
    monkeypatch.setenv("NOVEL_MEMORY_SCOPE_PATH", str(tmp_path / "state" / "active_scope.json"))

    store.set_scope("test_lib", ["memory_01"])
    assert store.active_scope()["memory_ids"] == ["memory_01"]

    store.delete_library_memory("test_lib", "memory_01")
    assert store.active_scope()["memory_ids"] == []
    with pytest.raises(ValueError, match="not found"):
        store.get_library_memory("test_lib", "memory_01")


def test_create_and_update_library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = setup_test_store(tmp_path)
    monkeypatch.setenv("NOVEL_MEMORY_LIBRARIES_DIR", str(tmp_path / "libraries"))

    new_lib = store.create_library("new_novel", "新长篇小说", "硬科幻长篇大纲")
    assert new_lib["library_id"] == "new_novel"
    assert new_lib["name"] == "新长篇小说"

    updated_lib = store.update_library("new_novel", "新长篇小说·定稿版", "定稿大纲")
    assert updated_lib["name"] == "新长篇小说·定稿版"


def test_admin_api_endpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    setup_test_store(tmp_path)
    monkeypatch.setenv("NOVEL_MEMORY_LIBRARIES_DIR", str(tmp_path / "libraries"))
    monkeypatch.setenv("NOVEL_MEMORY_SCOPE_PATH", str(tmp_path / "state" / "active_scope.json"))
    monkeypatch.setenv("NOVEL_MEMORY_ADMIN_TOKEN", "test-admin-secret-token")
    monkeypatch.setenv("NOVEL_MEMORY_WEB_DIR", str(tmp_path / "web"))
    (tmp_path / "web").mkdir(exist_ok=True)
    (tmp_path / "web" / "index.html").write_text("<h1>test</h1>", encoding="utf-8")

    client = TestClient(admin.create_app())

    # The management page itself must be gated by the login screen.
    res = client.get("/")
    assert res.status_code == 200
    assert "请输入管理密码" in res.text

    # Unauthorized requests should be rejected
    res = client.get("/api/libraries")
    assert res.status_code == 401

    auth_headers = {"Authorization": "Bearer test-admin-secret-token"}

    res = client.post("/api/login", json={"token": "test-admin-secret-token"})
    assert res.status_code == 200
    assert "novel_memory_session" in res.headers.get("set-cookie", "")
    res = client.get("/")
    assert "<h1>test</h1>" in res.text

    # List libraries
    res = client.get("/api/libraries", headers=auth_headers)
    assert res.status_code == 200
    libs = res.json()
    assert any(lib["library_id"] == "test_lib" for lib in libs)

    # Create new library via API
    res = client.post(
        "/api/libraries",
        headers=auth_headers,
        json={"library_id": "api_novel", "name": "API小说", "description": "API简介"},
    )
    assert res.status_code == 201

    # Update library via API
    res = client.put(
        "/api/libraries/api_novel",
        headers=auth_headers,
        json={"name": "API小说修改", "description": "修改简介"},
    )
    assert res.status_code == 200
    assert res.json()["name"] == "API小说修改"

    # Create memory via API
    res = client.post(
        "/api/libraries/api_novel/memories",
        headers=auth_headers,
        json={
            "memory_id": "stage_1",
            "date_range": "2000年",
            "title": "起点",
            "tags": ["序曲"],
            "content": "一切的开端正文。",
        },
    )
    assert res.status_code == 201

    # Read memory via API
    res = client.get("/api/libraries/api_novel/memories/stage_1", headers=auth_headers)
    assert res.status_code == 200
    assert res.json()["content"] == "一切的开端正文。"
    initial_digest = res.json()["digest"]

    # Update memory via API
    res = client.put(
        "/api/libraries/api_novel/memories/stage_1",
        headers=auth_headers,
        json={
            "date_range": "2000年—2001年",
            "title": "起点修订",
            "tags": ["序曲", "新标签"],
            "content": "修改后的正文内容。",
            "expected_digest": initial_digest,
        },
    )
    assert res.status_code == 200
    assert res.json()["title"] == "起点修订"
    updated_digest = res.json()["digest"]

    # Delete memory via API
    res = client.request(
        "DELETE", "/api/libraries/api_novel/memories/stage_1", headers=auth_headers,
        json={"expected_digest": updated_digest},
    )
    assert res.status_code == 200
    assert res.json()["ok"] is True

    # Upload raw markdown with front matter
    res = client.post(
        "/api/libraries/api_novel/memories",
        headers=auth_headers,
        json={
            "filename": "chapter_05.md",
            "raw_markdown": "---\nid: stage_05\ndate_range: 2005年\ntitle: 第五卷决战\ntags: 决战, 终章\n---\n\n这是直接上传的原始 Markdown 文件正文。\n",
        },
    )
    assert res.status_code == 201
    assert res.json()["memory_id"] == "stage_05"
    assert res.json()["title"] == "第五卷决战"
    assert res.json()["content"] == "这是直接上传的原始 Markdown 文件正文。"

    # Upload plain raw markdown without front matter
    res = client.post(
        "/api/libraries/api_novel/memories",
        headers=auth_headers,
        json={
            "filename": "simple_note.md",
            "raw_markdown": "# 示例草稿标题\n\n普通的草稿笔记内容，没有前置元数据。",
        },
    )
    assert res.status_code == 201
    assert res.json()["title"] == "示例草稿标题"
    assert "普通的草稿笔记内容" in res.json()["content"]

    # Test auto-generated library_id when omitted
    res = client.post(
        "/api/libraries",
        headers=auth_headers,
        json={"name": "无ID小说", "description": "系统自动生成ID"},
    )
    assert res.status_code == 201
    auto_lib_id = res.json()["library_id"]
    assert auto_lib_id.startswith("novel_")
    assert res.json()["name"] == "无ID小说"

    # Test change admin token
    res = client.post(
        "/api/token",
        headers=auth_headers,
        json={"new_token": "brand-new-secret-token-123"},
    )
    assert res.status_code == 200
    assert res.json()["ok"] is True

    # Old token should be rejected now
    res = client.get("/api/libraries", headers=auth_headers)
    assert res.status_code == 401

    # New token should be accepted
    new_auth_headers = {"Authorization": "Bearer brand-new-secret-token-123"}
    res = client.get("/api/libraries", headers=new_auth_headers)
    assert res.status_code == 200
