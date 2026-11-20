from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from novel_memory_mcp import admin, files
from novel_memory_mcp.store import ConflictError, MemoryStore, ScopeError


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for name, value in {
        "NOVEL_MEMORY_DIR": tmp_path / "memories",
        "NOVEL_MEMORY_LIBRARIES_DIR": tmp_path / "libraries",
        "NOVEL_MEMORY_INDEX_PATH": tmp_path / "state" / "index.sqlite3",
        "NOVEL_MEMORY_SCOPE_PATH": tmp_path / "state" / "scope.json",
        "NOVEL_MEMORY_ADMIN_TOKEN_PATH": tmp_path / "state" / "admin_token.txt",
        "NOVEL_MEMORY_WEB_DIR": tmp_path / "web",
    }.items():
        monkeypatch.setenv(name, str(value))
    monkeypatch.setenv("NOVEL_MEMORY_ADMIN_TOKEN", "fictional-test-password-123456")
    (tmp_path / "memories").mkdir()
    (tmp_path / "web").mkdir()
    (tmp_path / "web" / "index.html").write_text("<h1>Test console</h1>", encoding="utf-8")
    store = MemoryStore.from_environment()
    store.create_library("demo", "虚构示例")
    headers = {"Authorization": "Bearer fictional-test-password-123456"}
    with TestClient(admin.create_app(), raise_server_exceptions=False) as client:
        yield store, client, headers


def create(store: MemoryStore, memory_id: str = "one", content: str = "Original fictional body"):
    return store.save_library_memory("demo", memory_id, "day 1", "Test stage", ["test"], content, is_new=True)


def test_chinese_filenames_do_not_collide_and_reimport_is_explicit(setup):
    store, client, headers = setup
    responses = [client.post("/api/libraries/demo/memories", headers=headers, json={
        "filename": name, "raw_markdown": body,
    }) for name, body in [("第一章.md", "Fictional A"), ("第二章.md", "Fictional B")]]
    assert [response.status_code for response in responses] == [201, 201]
    assert len({response.json()["memory_id"] for response in responses}) == 2
    repeated = client.post("/api/libraries/demo/memories", headers=headers, json={
        "filename": "第一章.md", "raw_markdown": "Replacement",
    })
    assert repeated.status_code == 409
    assert store.get_library_memory("demo", responses[0].json()["memory_id"])["content"] == "Fictional A"


def test_invalid_metadata_does_not_change_file_or_catalog(setup):
    store, client, headers = setup
    old = create(store)
    path = Path(old["path"])
    before = path.read_bytes()
    response = client.put("/api/libraries/demo/memories/one", headers=headers, json={
        "date_range": "day 2", "title": "Invalid\nmetadata", "content": "Replacement",
        "expected_digest": old["digest"],
    })
    assert response.status_code == 400
    assert path.read_bytes() == before
    assert client.get("/api/libraries", headers=headers).status_code == 200


def test_atomic_failure_preserves_original_and_cleans_pending_file(setup, monkeypatch):
    store, _, _ = setup
    old = create(store)
    target = Path(old["path"])
    before = target.read_bytes()
    replace = files.os.replace

    def failing_replace(source, destination):
        if Path(destination) == target:
            raise OSError("synthetic write failure")
        return replace(source, destination)

    monkeypatch.setattr(files.os, "replace", failing_replace)
    with pytest.raises(OSError):
        store.save_library_memory("demo", "one", "day 2", "Updated", [], "Replacement", expected_digest=old["digest"])
    assert target.read_bytes() == before
    assert not list(target.parent.glob(".one.md.*"))
    assert len(list((store.index_path.parent / "history" / "demo" / "one").glob("*.md"))) == 1


@pytest.mark.parametrize("corrupt", ["{", "[]", '{"library_id":"../other","memory_ids":[]}',
                                    '{"library_id":"demo","memory_ids":"one"}'])
def test_invalid_scope_never_opens_fallback_and_console_can_repair(setup, corrupt):
    store, client, headers = setup
    create(store)
    fallback = store.memory_dir / "fallback.md"
    fallback.write_text("---\nid: fallback\ndate_range: test\ntitle: test\n---\n\nFallback data", encoding="utf-8")
    store.set_scope("demo", ["one"])
    store.scope_path.write_text(corrupt, encoding="utf-8")
    with pytest.raises(ScopeError):
        store.list_tags()
    assert client.get("/api/scope", headers=headers).status_code == 503
    assert client.get("/api/libraries", headers=headers).status_code == 200
    assert client.post("/api/scope", headers=headers, json={"library_id": "demo", "memory_ids": ["one"]}).status_code == 200
    assert [item["memory_id"] for item in store.list_tags()] == ["one"]


def test_scope_update_failure_keeps_previous_valid_scope(setup, monkeypatch):
    store, _, _ = setup
    create(store)
    store.set_scope("demo", ["one"])
    before = store.scope_path.read_bytes()
    replace = files.os.replace

    def failing_replace(source, destination):
        if Path(destination) == store.scope_path:
            raise OSError("synthetic scope failure")
        return replace(source, destination)

    monkeypatch.setattr(files.os, "replace", failing_replace)
    with pytest.raises(OSError):
        store.set_scope("demo", [])
    assert store.scope_path.read_bytes() == before


def test_chinese_password_uses_ascii_cookie_and_changes_auth_atomically(setup):
    store, client, headers = setup
    response = client.post("/api/token", headers=headers, json={"new_token": "虚构测试管理密码"})
    assert response.status_code == 200
    assert "虚构" not in response.headers["set-cookie"]
    assert client.get("/api/libraries").status_code == 200
    assert client.get("/api/libraries", headers=headers).status_code == 401
    client.cookies.clear()
    assert client.post("/api/login", json={"token": "虚构测试管理密码"}).status_code == 200
    assert client.get("/api/libraries").status_code == 200
    assert store.active_scope() == {"library_id": "", "memory_ids": []}


def test_password_write_failure_preserves_old_credential(setup, monkeypatch):
    _, client, headers = setup

    def fail(*args):
        raise OSError("synthetic password failure")

    monkeypatch.setattr(admin, "atomic_write", fail)
    assert client.post("/api/token", headers=headers, json={"new_token": "New fictional password"}).status_code == 503
    assert client.get("/api/libraries", headers=headers).status_code == 200


def test_bad_file_and_duplicate_ids_are_reported_without_hiding_valid_records(setup):
    store, client, headers = setup
    record = create(store)
    directory = Path(record["path"]).parent
    (directory / "bad.md").write_text("No metadata", encoding="utf-8")
    (directory / "duplicate_a.md").write_text(
        "---\nid: duplicate\ndate_range: day\ntitle: test\n---\n\nFictional A", encoding="utf-8",
    )
    (directory / "duplicate_b.md").write_text(
        "---\nid: duplicate\ndate_range: day\ntitle: test\n---\n\nFictional B", encoding="utf-8",
    )
    response = client.get("/api/libraries", headers=headers)
    assert response.status_code == 200
    assert len(response.json()[0]["errors"]) == 2
    assert [item["memory_id"] for item in response.json()[0]["memories"]] == ["one"]
    store.set_scope("demo", ["one"])
    assert store.get("one")["content"] == "Original fictional body"
    with pytest.raises(ValueError, match="unknown"):
        store.set_scope("demo", ["duplicate"])


def test_nested_filename_mismatch_can_be_read_updated_and_deleted(setup):
    store, client, headers = setup
    path = store.library_root / "demo" / "memories" / "archive" / "different.md"
    path.parent.mkdir()
    path.write_text("---\nid: nested\ndate_range: day\ntitle: test\n---\n\nFictional body", encoding="utf-8")
    response = client.get("/api/libraries/demo/memories/nested", headers=headers)
    assert response.status_code == 200
    updated = client.put("/api/libraries/demo/memories/nested", headers=headers, json={
        "date_range": "day 2", "title": "Updated", "content": "New body",
        "expected_digest": response.json()["digest"],
    })
    assert updated.status_code == 200
    assert "New body" in path.read_text(encoding="utf-8")
    deleted = client.request("DELETE", "/api/libraries/demo/memories/nested", headers=headers,
                             json={"expected_digest": updated.json()["digest"]})
    assert deleted.status_code == 200
    assert not path.exists()
    assert len(list((store.index_path.parent / "history" / "demo" / "nested").glob("*.md"))) == 2


@pytest.mark.parametrize("endpoint", ["/api/login", "/api/libraries", "/api/scope", "/api/token"])
@pytest.mark.parametrize("value", [[], None, "text"])
def test_json_shape_errors_are_client_errors(setup, endpoint, value):
    _, client, headers = setup
    response = client.post(endpoint, headers=headers, content=json.dumps(value))
    assert response.status_code == 400


def test_false_string_and_null_fields_never_modify_records(setup):
    store, client, headers = setup
    original = create(store)
    for extra in ({"overwrite": "false"}, {"title": None}, {"tags": [None]}):
        body = {"memory_id": "one", "date_range": "day 2", "title": "Updated", "content": "New", **extra}
        assert client.post("/api/libraries/demo/memories", headers=headers, json=body).status_code == 400
    assert store.get_library_memory("demo", "one")["digest"] == original["digest"]


def test_utf8_bom_and_quoted_metadata_round_trip(setup):
    store, _, _ = setup
    imported = store.import_raw_markdown("demo", "bom.md", '\ufeff---\nid: bom_record\ndate_range: day\ntitle: test\n---\n\nBody')
    assert imported["memory_id"] == "bom_record"
    path = Path(imported["path"])
    path.write_text("\ufeff" + path.read_text(encoding="utf-8"), encoding="utf-8")
    assert store.get_library_memory("demo", "bom_record")["content"] == "Body"
    updated = store.save_library_memory("demo", "bom_record", "day", '"Literal quoted title"', ["tag"], "Body")
    assert updated["title"] == '"Literal quoted title"'


def test_stale_api_update_and_delete_are_rejected(setup):
    store, client, headers = setup
    original = create(store)
    endpoint = "/api/libraries/demo/memories/one"
    body = {"date_range": "day", "title": "Updated", "content": "First edit", "expected_digest": original["digest"]}
    assert client.put(endpoint, headers=headers, json=body).status_code == 200
    assert client.put(endpoint, headers=headers, json={**body, "content": "Stale edit"}).status_code == 409
    assert client.request("DELETE", endpoint, headers=headers, json={"expected_digest": original["digest"]}).status_code == 409
    assert client.put(endpoint, headers=headers, json={"content": "No version"}).status_code == 428
    assert store.get_library_memory("demo", "one")["content"] == "First edit"


def test_two_cooperating_editors_cannot_both_save_same_digest(setup):
    store, _, _ = setup
    original = create(store)

    def update(body):
        try:
            store.save_library_memory("demo", "one", "day", "test", [], body, expected_digest=original["digest"])
            return "saved"
        except ConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(update, ["First edit", "Second edit"])) == ["conflict", "saved"]


def test_one_scope_snapshot_covers_scan_index_and_final_read(setup, monkeypatch):
    store, _, _ = setup
    create(store, "shared", "Body from A")
    store.create_library("other", "Other fictional book")
    store.save_library_memory("other", "shared", "day", "test", [], "Body from B", is_new=True)
    store.set_scope("demo", ["shared"])
    scan = store._scan

    def switch_after_scan(scope):
        blocks = scan(scope)
        store.set_scope("other", ["shared"])
        return blocks

    monkeypatch.setattr(store, "_scan", switch_after_scan)
    assert store.get("shared")["content"] == "Body from A"
    monkeypatch.setattr(store, "_scan", scan)
    assert store.get("shared")["content"] == "Body from B"


def test_all_read_tools_obey_empty_and_partial_scope(setup):
    store, _, _ = setup
    create(store, "one", "Shared keyword A")
    create(store, "two", "Shared keyword B")
    store.set_scope("demo", ["one"])
    assert [row["memory_id"] for row in store.list_tags()] == ["one"]
    assert [row["memory_id"] for row in store.search("Shared", 5)] == ["one"]
    with pytest.raises(ValueError):
        store.get("two")
    with pytest.raises(ValueError):
        store.get_many(["one", "two"])
    store.set_scope("demo", [])
    assert store.list_tags() == []
    assert store.search("Shared", 5) == []
    with pytest.raises(ValueError):
        store.get("one")


def test_invalid_utf8_scope_is_explicitly_closed(setup):
    store, client, headers = setup
    create(store)
    store.set_scope("demo", ["one"])
    store.scope_path.write_bytes(b"\xff")
    with pytest.raises(ScopeError):
        store.list_tags()
    assert client.get("/api/scope", headers=headers).status_code == 503


def test_invalid_unicode_login_never_crashes_or_changes_credential(setup):
    _, client, headers = setup
    assert client.post("/api/login", content='{"token":"\\ud800"}').status_code == 401
    assert client.get("/api/libraries", headers=headers).status_code == 200


def test_index_accepts_swapped_file_ids_without_unique_path_failure(setup):
    store, _, _ = setup
    first = create(store, "one", "First fictional record")
    second = create(store, "two", "Second fictional record")
    store.set_scope("demo", ["one", "two"])
    assert len(store.list_tags()) == 2
    for record, old, new in [(first, "one", "two"), (second, "two", "one")]:
        path = Path(record["path"])
        path.write_text(path.read_text(encoding="utf-8").replace(f"id: {old}\n", f"id: {new}\n"), encoding="utf-8")
    assert store.get("one")["content"] == "Second fictional record"
    assert store.get("two")["content"] == "First fictional record"


def test_explicit_import_replacement_requires_and_checks_current_digest(setup):
    store, client, headers = setup
    original = create(store)
    body = {"filename": "one.md", "raw_markdown": "---\nid: one\ndate_range: day\ntitle: replaced\n---\n\nReplacement",
            "overwrite": True}
    endpoint = "/api/libraries/demo/memories"
    assert client.post(endpoint, headers=headers, json=body).status_code == 428
    body["expected_digest"] = original["digest"]
    assert client.post(endpoint, headers=headers, json=body).status_code == 201
    assert client.post(endpoint, headers=headers, json=body).status_code == 409
    assert store.get_library_memory("demo", "one")["content"] == "Replacement"
    backups = list((store.index_path.parent / "history" / "demo" / "one").glob("*.md"))
    assert len(backups) == 1
    assert "Original fictional body" in backups[0].read_text(encoding="utf-8")


def test_https_session_cookie_is_secure_and_does_not_contain_password(setup):
    _, _, _ = setup
    with TestClient(admin.create_app(), base_url="https://testserver") as client:
        response = client.post("/api/login", json={"token": "fictional-test-password-123456"})
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert "Secure" in cookie
        assert "fictional-test-password" not in cookie
        assert client.get("/api/libraries").status_code == 200


def test_backups_are_not_indexed_when_state_is_configured_inside_a_library(setup):
    store, _, _ = setup
    original = create(store)
    store.index_path = Path(original["path"]).parent / "state" / "index.sqlite3"
    updated = store.save_library_memory("demo", "one", "day", "Updated", [], "New body",
                                        expected_digest=original["digest"])
    assert updated["content"] == "New body"
    assert len(store.list_library_memories("demo")) == 1
    store.set_scope("demo", ["one"])
    assert store.get("one")["content"] == "New body"
