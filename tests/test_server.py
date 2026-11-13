from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from novel_memory_mcp import server
from novel_memory_mcp.store import MemoryStore


def write_memory(directory: Path, memory_id: str, date_range: str, title: str, tags: str, content: str) -> None:
    (directory / f"{memory_id}.md").write_text(
        f"---\nid: {memory_id}\ndate_range: {date_range}\ntitle: {title}\ntags: {tags}\n---\n\n{content}\n",
        encoding="utf-8",
    )


def build_store(tmp_path: Path) -> MemoryStore:
    memory_dir = tmp_path / "memories"
    memory_dir.mkdir()
    write_memory(
        memory_dir,
        "demo_stage_01",
        "故事第1天—第3天",
        "旅人抵达河港，开始修桥",
        "河港, 修桥, 木材, 角色丙, 课程",
        "角色甲在河港领取木材清单。角色丙讲解课程，记录桥面观察结果。",
    )
    write_memory(
        memory_dir,
        "demo_stage_02",
        "故事第4天—第6天",
        "灯塔修复，约定夜间信号",
        "灯塔区, 灯塔, B-01, 登记, 角色丙",
        "灯塔值守日志列出咨询安排、风暴风险和海岸边界三项检查。角色丙负责签收。",
    )
    return MemoryStore(memory_dir, tmp_path / "state" / "index.sqlite3")


def test_search_returns_only_stable_memory_metadata(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    result = store.search("灯塔", limit=5)
    assert result == [
        {
            "memory_id": "demo_stage_02",
            "date_range": "故事第4天—第6天",
            "title": "灯塔修复，约定夜间信号",
        }
    ]
    assert "content" not in result[0]


def test_get_returns_the_unchanged_memory_body(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    result = store.get("demo_stage_02")
    assert result["content"] == "灯塔值守日志列出咨询安排、风暴风险和海岸边界三项检查。角色丙负责签收。"
    assert result["title"] == "灯塔修复，约定夜间信号"


def test_store_refreshes_after_a_markdown_edit(tmp_path: Path) -> None:
    store = build_store(tmp_path)
    assert store.search("新关键词", limit=5) == []
    target = tmp_path / "memories" / "demo_stage_02.md"
    target.write_text(target.read_text(encoding="utf-8") + "\n新关键词\n", encoding="utf-8")
    assert store.search("新关键词", limit=5)[0]["memory_id"] == "demo_stage_02"


def test_multi_keyword_search_matches_any_term_and_ranks_coverage(tmp_path: Path) -> None:
    result = build_store(tmp_path).search("角色丙 观察 咨询 风暴风险 海岸边界 课程", limit=5)
    assert [item["memory_id"] for item in result] == [
        "demo_stage_02",
        "demo_stage_01",
    ]


def test_get_rejects_an_invalid_memory_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="memory_id"):
        build_store(tmp_path).get("../secrets")


def test_search_memory_validates_query(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(server, "_store", lambda: build_store(tmp_path))
    result = server.search_memory("灯塔")
    assert result["items"][0]["memory_id"] == "demo_stage_02"
    with pytest.raises(ValueError, match="must not be blank"):
        server.search_memory(" ")
    with pytest.raises(ValueError, match="must not exceed"):
        server.search_memory("x" * (server.MAX_QUERY_CHARS + 1))


def test_list_memory_tags_returns_compact_index(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(server, "_store", lambda: build_store(tmp_path))
    result = server.list_memory_tags()
    assert result["items"] == [
        {
            "memory_id": "demo_stage_01",
            "date_range": "故事第1天—第3天",
            "title": "旅人抵达河港，开始修桥",
            "tags": ["河港", "修桥", "木材", "角色丙", "课程"],
        },
        {
            "memory_id": "demo_stage_02",
            "date_range": "故事第4天—第6天",
            "title": "灯塔修复，约定夜间信号",
            "tags": ["灯塔区", "灯塔", "B-01", "登记", "角色丙"],
        },
    ]


def test_get_memories_reads_at_most_two_selected_blocks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    store = build_store(tmp_path)
    monkeypatch.setattr(server, "_store", lambda: store)
    result = server.get_memories(
        ["demo_stage_01", "demo_stage_02"]
    )
    assert [item["memory_id"] for item in result["items"]] == [
        "demo_stage_01",
        "demo_stage_02",
    ]
    assert all("content" in item for item in result["items"])
    with pytest.raises(ValueError, match="at most 2"):
        server.get_memories(
            [
                "demo_stage_01",
                "demo_stage_02",
                "demo_stage_01",
            ]
        )


def test_http_transport_is_stateless_for_read_only_tools() -> None:
    assert server.mcp.settings.stateless_http is True
    assert "先调用 list_memory_tags" in (server.mcp.instructions or "")


def test_http_token_is_required_and_must_be_strong() -> None:
    with pytest.raises(RuntimeError, match="required"):
        server._validated_http_token("")
    with pytest.raises(RuntimeError, match="at least"):
        server._validated_http_token("short")
    with pytest.raises(RuntimeError, match="Replace"):
        server._validated_http_token("replace-with-a-long-random-token")
    token = "a" * server.MIN_HTTP_TOKEN_CHARS
    assert server._validated_http_token(token) == token


def test_bearer_guard_rejects_missing_token() -> None:
    called = False
    sent: list[dict] = []

    async def app(scope, receive, send):
        nonlocal called
        called = True

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    guarded = server._bearer_guard(app, "x" * server.MIN_HTTP_TOKEN_CHARS)
    asyncio.run(guarded({"type": "http", "headers": []}, receive, send))
    assert called is False
    assert sent[0]["status"] == 401


def test_bearer_guard_accepts_matching_token() -> None:
    called = False
    token = "x" * server.MIN_HTTP_TOKEN_CHARS

    async def app(scope, receive, send):
        nonlocal called
        called = True

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        raise AssertionError(f"guard unexpectedly sent {message!r}")

    guarded = server._bearer_guard(app, token)
    asyncio.run(
        guarded(
            {"type": "http", "headers": [(b"authorization", f"Bearer {token}".encode())]},
            receive,
            send,
        )
    )
    assert called is True


def test_scope_filters_all_read_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    library_root = tmp_path / "libraries"
    directory = library_root / "demo_story" / "memories"
    directory.mkdir(parents=True)
    for number in (1, 2):
        write_memory(directory, f"stage_{number}", "示例时间", "共同标题", "共同标签", f"示例正文{number}")
    monkeypatch.setenv("NOVEL_MEMORY_LIBRARIES_DIR", str(library_root))
    monkeypatch.setenv("NOVEL_MEMORY_SCOPE_PATH", str(tmp_path / "state" / "scope.json"))
    store = MemoryStore(tmp_path / "memories", tmp_path / "state" / "index.sqlite3")
    store.set_scope("demo_story", ["stage_1"])
    assert [row["memory_id"] for row in store.list_tags()] == ["stage_1"]
    assert [row["memory_id"] for row in store.search("共同", 5)] == ["stage_1"]
    assert store.get("stage_1")["content"] == "示例正文1"
    with pytest.raises(ValueError, match="not found"):
        store.get("stage_2")
    with pytest.raises(ValueError, match="not found"):
        store.get_many(["stage_1", "stage_2"])
    store.set_scope("demo_story", [])
    assert store.list_tags() == []
    assert store.search("共同", 5) == []
    with pytest.raises(ValueError, match="not found"):
        store.get("stage_1")
