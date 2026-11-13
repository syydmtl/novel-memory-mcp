"""Local Markdown source and SQLite FTS5 index for novel-memory blocks."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

MEMORY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
FRONT_MATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.DOTALL)
QUERY_SEPARATOR_RE = re.compile(r"[\s,，、;；|/]+")
REQUIRED_FIELDS = ("id", "date_range", "title")


@dataclass(frozen=True)
class MemoryBlock:
    memory_id: str
    date_range: str
    title: str
    tags: str
    content: str
    path: str
    digest: str


class MemoryStore:
    """Keeps Markdown as the sole source of truth and SQLite as a rebuildable index."""

    def __init__(self, memory_dir: Path, index_path: Path) -> None:
        self.memory_dir = memory_dir.resolve()
        self.index_path = index_path.resolve()

    @property
    def library_root(self) -> Path:
        configured = os.getenv("NOVEL_MEMORY_LIBRARIES_DIR", "").strip()
        return Path(configured).resolve() if configured else self.memory_dir.parent / "libraries"

    @property
    def scope_path(self) -> Path:
        configured = os.getenv("NOVEL_MEMORY_SCOPE_PATH", "").strip()
        return Path(configured).resolve() if configured else self.index_path.parent / "active_scope.json"

    @classmethod
    def from_environment(cls) -> MemoryStore:
        memory_dir = Path(os.getenv("NOVEL_MEMORY_DIR", "memories"))
        index_path = Path(os.getenv("NOVEL_MEMORY_INDEX_PATH", "state/novel_memory_index.sqlite3"))
        return cls(memory_dir, index_path)

    def _connect(self) -> sqlite3.Connection:
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.index_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS memories (
                memory_id TEXT PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                date_range TEXT NOT NULL,
                title TEXT NOT NULL,
                tags TEXT NOT NULL,
                content TEXT NOT NULL,
                digest TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                memory_id UNINDEXED,
                title,
                tags,
                content,
                tokenize='unicode61'
            )
            """
        )
        return connection

    def _read_block(self, path: Path) -> MemoryBlock | None:
        if path.name.lower() == "readme.md" or path.name.startswith("."):
            return None
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{path}: memory files must be UTF-8 Markdown") from exc
        match = FRONT_MATTER_RE.match(text)
        if not match:
            raise ValueError(f"{path}: missing YAML front matter")
        metadata = self._parse_metadata(match.group(1), path)
        missing = [field for field in REQUIRED_FIELDS if not metadata.get(field)]
        if missing:
            raise ValueError(f"{path}: missing required metadata: {', '.join(missing)}")
        memory_id = metadata["id"]
        if not MEMORY_ID_RE.fullmatch(memory_id):
            raise ValueError(f"{path}: id must contain only letters, digits, underscores, or hyphens")
        # The newline immediately after front matter is formatting, not memory content.
        content = match.group(2).lstrip("\r\n").rstrip()
        if not content:
            raise ValueError(f"{path}: content must not be blank")
        return MemoryBlock(
            memory_id=memory_id,
            date_range=metadata["date_range"],
            title=metadata["title"],
            tags=metadata.get("tags", ""),
            content=content,
            path=str(path.resolve()),
            digest=hashlib.sha256(raw).hexdigest(),
        )

    @staticmethod
    def _parse_metadata(raw: str, path: Path) -> dict[str, str]:
        metadata: dict[str, str] = {}
        for line in raw.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if ":" not in line:
                raise ValueError(f"{path}: metadata must use key: value lines")
            key, value = line.split(":", 1)
            key, value = key.strip(), value.strip()
            if key in metadata:
                raise ValueError(f"{path}: duplicate metadata key: {key}")
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            metadata[key] = value
        return metadata

    def _scan(self) -> list[MemoryBlock]:
        memory_dir = self._active_memory_dir()
        if not memory_dir.is_dir():
            raise RuntimeError(f"memory directory does not exist or is not a directory: {memory_dir}")
        blocks: list[MemoryBlock] = []
        ids: set[str] = set()
        for path in sorted(memory_dir.rglob("*.md")):
            block = self._read_block(path)
            if block is None:
                continue
            if block.memory_id in ids:
                raise ValueError(f"duplicate memory id: {block.memory_id}")
            ids.add(block.memory_id)
            blocks.append(block)
        return blocks

    def list_libraries(self) -> list[dict[str, object]]:
        root = self.library_root
        if not root.is_dir():
            return []
        result = []
        for directory in sorted(path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")):
            metadata_path = directory / "library.json"
            metadata: dict[str, object] = {}
            if metadata_path.is_file():
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            memory_dir = directory / "memories"
            count = len(list(memory_dir.rglob("*.md"))) if memory_dir.is_dir() else 0
            result.append({
                "library_id": directory.name,
                "name": str(metadata.get("name", directory.name)),
                "description": str(metadata.get("description", "")),
                "memory_count": count,
            })
        return result

    def list_library_memories(self, library_id: str) -> list[dict[str, object]]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", library_id):
            raise ValueError("invalid library_id")
        directory = self.library_root / library_id / "memories"
        if not directory.is_dir():
            raise ValueError(f"library not found: {library_id}")
        blocks = [self._read_block(path) for path in sorted(directory.rglob("*.md"))]
        return [
            {
                "memory_id": block.memory_id,
                "date_range": block.date_range,
                "title": block.title,
                "tags": [tag.strip() for tag in block.tags.split(",") if tag.strip()],
                "content_length": len(block.content),
                "content_preview": block.content[:120].strip(),
            }
            for block in blocks if block is not None
        ]

    def get_library_memory(self, library_id: str, memory_id: str) -> dict[str, object]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", library_id):
            raise ValueError("invalid library_id")
        if not MEMORY_ID_RE.fullmatch(memory_id):
            raise ValueError("invalid memory_id")
        directory = self.library_root / library_id / "memories"
        target = directory / f"{memory_id}.md"
        if not target.is_file():
            raise ValueError(f"memory not found: {memory_id}")
        block = self._read_block(target)
        if block is None:
            raise ValueError(f"cannot read memory block: {memory_id}")
        return {
            "memory_id": block.memory_id,
            "date_range": block.date_range,
            "title": block.title,
            "tags": [tag.strip() for tag in block.tags.split(",") if tag.strip()],
            "raw_tags": block.tags,
            "content": block.content,
            "path": block.path,
            "digest": block.digest,
        }

    def save_library_memory(
        self,
        library_id: str,
        memory_id: str,
        date_range: str,
        title: str,
        tags: str | list[str],
        content: str,
        is_new: bool = False,
        overwrite: bool = False,
    ) -> dict[str, object]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", library_id):
            raise ValueError("invalid library_id")
        if not MEMORY_ID_RE.fullmatch(memory_id):
            raise ValueError("invalid memory_id")
        date_range = date_range.strip()
        title = title.strip()
        content = content.strip()
        if not date_range:
            raise ValueError("date_range must not be blank")
        if not title:
            raise ValueError("title must not be blank")
        if not content:
            raise ValueError("content must not be blank")

        directory = self.library_root / library_id / "memories"
        if not directory.is_dir():
            raise ValueError(f"library not found: {library_id}")
        target = directory / f"{memory_id}.md"
        if is_new and target.exists() and not overwrite:
            raise ValueError(f"memory already exists: {memory_id}")
        if not is_new and not target.is_file() and not overwrite:
            raise ValueError(f"memory not found: {memory_id}")

        if isinstance(tags, list):
            tags_str = ", ".join(str(t).strip() for t in tags if str(t).strip())
        else:
            tags_str = str(tags).strip()

        markdown_text = (
            f"---\n"
            f"id: {memory_id}\n"
            f"date_range: {date_range}\n"
            f"title: {title}\n"
            f"tags: {tags_str}\n"
            f"---\n\n"
            f"{content}\n"
        )
        target.write_text(markdown_text, encoding="utf-8")
        return self.get_library_memory(library_id, memory_id)

    def import_raw_markdown(
        self,
        library_id: str,
        filename: str,
        raw_text: str,
        overwrite: bool = False,
    ) -> dict[str, object]:
        """Import an existing Markdown file directly, extracting front matter if present."""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", library_id):
            raise ValueError("invalid library_id")
        directory = self.library_root / library_id / "memories"
        if not directory.is_dir():
            raise ValueError(f"library not found: {library_id}")

        stem = Path(filename).stem
        clean_stem = re.sub(r"[^A-Za-z0-9_-]", "_", stem).strip("_")

        match = FRONT_MATTER_RE.match(raw_text)
        if match:
            meta = self._parse_metadata(match.group(1), Path(filename))
            memory_id = meta.get("id", clean_stem or "memory_imported")
            date_range = meta.get("date_range", "未标注时间")
            title = meta.get("title", stem or "未命名阶段")
            tags = meta.get("tags", "")
            content = match.group(2).lstrip("\r\n").rstrip()
        else:
            memory_id = clean_stem if MEMORY_ID_RE.fullmatch(clean_stem) else f"memory_{clean_stem}"
            date_range = "未标注时间"
            first_line = raw_text.strip().splitlines()[0] if raw_text.strip() else ""
            if first_line.startswith("#"):
                title = first_line.lstrip("# ").strip()
                content = raw_text.strip()
            else:
                title = stem or "未命名阶段"
                content = raw_text.strip()
            tags = ""

        if not MEMORY_ID_RE.fullmatch(memory_id):
            memory_id = f"mem_{int(hashlib.md5(filename.encode('utf-8')).hexdigest()[:8], 16)}"

        if not content:
            raise ValueError(f"{filename}: file content must not be blank")

        return self.save_library_memory(
            library_id=library_id,
            memory_id=memory_id,
            date_range=date_range,
            title=title,
            tags=tags,
            content=content,
            is_new=True,
            overwrite=overwrite,
        )

    def delete_library_memory(self, library_id: str, memory_id: str) -> bool:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", library_id):
            raise ValueError("invalid library_id")
        if not MEMORY_ID_RE.fullmatch(memory_id):
            raise ValueError("invalid memory_id")
        directory = self.library_root / library_id / "memories"
        target = directory / f"{memory_id}.md"
        if not target.is_file():
            raise ValueError(f"memory not found: {memory_id}")
        target.unlink()

        scope = self._scope()
        if scope.get("library_id") == library_id and memory_id in scope.get("memory_ids", []):
            remaining = [m for m in scope.get("memory_ids", []) if m != memory_id]
            self.scope_path.parent.mkdir(parents=True, exist_ok=True)
            self.scope_path.write_text(
                json.dumps({"library_id": library_id, "memory_ids": remaining}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return True

    def create_library(self, library_id: str, name: str, description: str = "") -> dict[str, object]:
        name = name.strip()
        if not name:
            raise ValueError("name must not be blank")
        description = description.strip()

        if not library_id or not library_id.strip():
            import time
            library_id = f"novel_{int(time.time())}"
        elif not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", library_id):
            raise ValueError("invalid library_id: must use alphanumeric, dash or underscore")

        root = self.library_root
        root.mkdir(parents=True, exist_ok=True)
        library_dir = root / library_id
        if library_dir.exists():
            raise ValueError(f"library already exists: {library_id}")
        memory_dir = library_dir / "memories"
        memory_dir.mkdir(parents=True, exist_ok=True)
        metadata = {
            "library_id": library_id,
            "name": name,
            "description": description,
        }
        (library_dir / "library.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return {
            "library_id": library_id,
            "name": name,
            "description": description,
            "memory_count": 0,
        }

    def update_library(self, library_id: str, name: str, description: str = "") -> dict[str, object]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", library_id):
            raise ValueError("invalid library_id")
        name = name.strip()
        if not name:
            raise ValueError("name must not be blank")
        description = description.strip()

        library_dir = self.library_root / library_id
        if not library_dir.is_dir():
            raise ValueError(f"library not found: {library_id}")
        metadata_path = library_dir / "library.json"
        metadata: dict[str, object] = {}
        if metadata_path.is_file():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
        metadata["library_id"] = library_id
        metadata["name"] = name
        metadata["description"] = description
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        memory_dir = library_dir / "memories"
        count = len(list(memory_dir.rglob("*.md"))) if memory_dir.is_dir() else 0
        return {
            "library_id": library_id,
            "name": name,
            "description": description,
            "memory_count": count,
        }

    def _scope(self) -> dict[str, object]:
        if not self.scope_path.is_file():
            return {"library_id": "", "memory_ids": []}
        try:
            value = json.loads(self.scope_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {"library_id": "", "memory_ids": []}
        except (OSError, json.JSONDecodeError):
            return {"library_id": "", "memory_ids": []}

    def active_scope(self) -> dict[str, object]:
        scope = self._scope()
        library_id = str(scope.get("library_id", ""))
        ids = scope.get("memory_ids", [])
        return {"library_id": library_id, "memory_ids": [str(item) for item in ids] if isinstance(ids, list) else []}

    def set_scope(self, library_id: str, memory_ids: list[str]) -> dict[str, object]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", library_id):
            raise ValueError("invalid library_id")
        library_dir = self.library_root / library_id
        memory_dir = library_dir / "memories"
        if not memory_dir.is_dir():
            raise ValueError(f"library not found: {library_id}")
        available = {block.memory_id for path in memory_dir.rglob("*.md") if (block := self._read_block(path)) is not None}
        if not set(memory_ids).issubset(available):
            raise ValueError("scope contains an unknown memory_id")
        value = {"library_id": library_id, "memory_ids": list(dict.fromkeys(memory_ids))}
        self.scope_path.parent.mkdir(parents=True, exist_ok=True)
        self.scope_path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return value

    def _active_memory_dir(self) -> Path:
        scope = self.active_scope()
        library_id = str(scope.get("library_id", ""))
        if library_id:
            directory = self.library_root / library_id / "memories"
            if not directory.is_dir():
                raise RuntimeError(f"active library does not exist: {library_id}")
            return directory
        return self.memory_dir

    def _sync(self, connection: sqlite3.Connection) -> None:
        blocks = self._scan()
        current = {block.memory_id: block for block in blocks}
        existing = {
            row["memory_id"]: row["digest"]
            for row in connection.execute("SELECT memory_id, digest FROM memories")
        }
        with connection:
            for memory_id in set(existing) - set(current):
                connection.execute("DELETE FROM memories WHERE memory_id = ?", (memory_id,))
                connection.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,))
            for block in blocks:
                if existing.get(block.memory_id) == block.digest:
                    continue
                connection.execute("DELETE FROM memory_fts WHERE memory_id = ?", (block.memory_id,))
                connection.execute(
                    """
                    INSERT INTO memories(memory_id, path, date_range, title, tags, content, digest)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(memory_id) DO UPDATE SET
                        path=excluded.path, date_range=excluded.date_range, title=excluded.title,
                        tags=excluded.tags, content=excluded.content, digest=excluded.digest
                    """,
                    (
                        block.memory_id,
                        block.path,
                        block.date_range,
                        block.title,
                        block.tags,
                        block.content,
                        block.digest,
                    ),
                )
                connection.execute(
                    "INSERT INTO memory_fts(memory_id, title, tags, content) VALUES (?, ?, ?, ?)",
                    (block.memory_id, block.title, block.tags, block.content),
                )

    @staticmethod
    def _query_terms(query: str) -> list[str]:
        """Split model-generated keyword lists without breaking identifiers such as A-17."""
        terms: list[str] = []
        seen: set[str] = set()
        for raw_term in QUERY_SEPARATOR_RE.split(query):
            term = raw_term.strip().strip('"\'')
            key = term.casefold()
            if term and key not in seen:
                terms.append(term)
                seen.add(key)
        return terms

    @staticmethod
    def _score_row(row: sqlite3.Row, terms: list[str]) -> tuple[int, int]:
        """Prefer stages covering more query clues, then title/tag matches."""
        fields = {
            "title": row["title"].casefold(),
            "tags": row["tags"].casefold(),
            "date_range": row["date_range"].casefold(),
            "content": row["content"].casefold(),
        }
        score = 0
        matched_terms = 0
        for term in terms:
            needle = term.casefold()
            weight = 0
            if needle in fields["title"]:
                weight += 8
            if needle in fields["tags"]:
                weight += 6
            if needle in fields["date_range"]:
                weight += 4
            if needle in fields["content"]:
                weight += 2
            if weight:
                matched_terms += 1
                score += weight
        # A stage that covers several independent clues should outrank one that
        # happens to contain only a single broad name such as CharacterA.
        return matched_terms * 20 + score, matched_terms

    def search(self, query: str, limit: int) -> list[dict[str, str]]:
        connection = self._connect()
        try:
            self._sync(connection)
            terms = self._query_terms(query)
            if not terms:
                return []

            # SQLite FTS5 remains the index for tokenized terms. The final
            # ranking below deliberately uses per-term substring coverage as
            # well, because Chinese aliases and model-generated keyword lists
            # are not reliably represented by a single FTS5 token.
            fts_query = " OR ".join(f'"{term.replace(chr(34), "")}"' for term in terms)
            fts_ids = {
                row["memory_id"]
                for row in connection.execute(
                    "SELECT memory_id FROM memory_fts WHERE memory_fts MATCH ?", (fts_query,)
                )
            }
            rows = connection.execute(
                "SELECT memory_id, date_range, title, tags, content FROM memories"
            ).fetchall()
            scope = self.active_scope()
            active_ids = set(scope.get("memory_ids", []))
            if scope.get("library_id"):
                rows = [row for row in rows if row["memory_id"] in active_ids]
            ranked: list[tuple[int, int, int, sqlite3.Row]] = []
            for row in rows:
                score, matched_terms = self._score_row(row, terms)
                if matched_terms:
                    # FTS contributes only a stable tie-breaker; it must not
                    # suppress valid Chinese substring matches.
                    ranked.append((score, matched_terms, int(row["memory_id"] in fts_ids), row))
            ranked.sort(key=lambda item: (-item[0], -item[1], -item[2], item[3]["memory_id"]))
            return [
                {
                    "memory_id": row["memory_id"],
                    "date_range": row["date_range"],
                    "title": row["title"],
                }
                for _, _, _, row in ranked[:limit]
            ]
        finally:
            connection.close()

    def list_tags(self) -> list[dict[str, str | list[str]]]:
        """Return compact discovery metadata for every maintained memory block."""
        connection = self._connect()
        try:
            self._sync(connection)
            rows = connection.execute(
                "SELECT memory_id, date_range, title, tags FROM memories ORDER BY memory_id"
            ).fetchall()
            scope = self.active_scope()
            active_ids = set(scope.get("memory_ids", []))
            if scope.get("library_id"):
                rows = [row for row in rows if row["memory_id"] in active_ids]
            return [
                {
                    "memory_id": row["memory_id"],
                    "date_range": row["date_range"],
                    "title": row["title"],
                    "tags": [tag.strip() for tag in row["tags"].split(",") if tag.strip()],
                }
                for row in rows
            ]
        finally:
            connection.close()

    def get(self, memory_id: str) -> dict[str, str]:
        if not MEMORY_ID_RE.fullmatch(memory_id):
            raise ValueError("memory_id must contain only letters, digits, underscores, or hyphens")
        connection = self._connect()
        try:
            self._sync(connection)
            row = connection.execute(
                "SELECT memory_id, date_range, title, content FROM memories WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            scope = self.active_scope()
            active_ids = set(scope.get("memory_ids", []))
            if row is None or (scope.get("library_id") and memory_id not in active_ids):
                raise ValueError(f"memory_id not found: {memory_id}")
            return dict(row)
        finally:
            connection.close()

    def get_many(self, memory_ids: list[str]) -> list[dict[str, str]]:
        """Return selected complete memory blocks in the requested order."""
        if not memory_ids:
            raise ValueError("memory_ids must not be empty")
        if len(memory_ids) > 2:
            raise ValueError("at most 2 memory_ids may be requested")
        if len(set(memory_ids)) != len(memory_ids):
            raise ValueError("memory_ids must be unique")
        for memory_id in memory_ids:
            if not MEMORY_ID_RE.fullmatch(memory_id):
                raise ValueError("memory_id must contain only letters, digits, underscores, or hyphens")

        connection = self._connect()
        try:
            self._sync(connection)
            placeholders = ", ".join("?" for _ in memory_ids)
            rows = connection.execute(
                f"SELECT memory_id, date_range, title, content FROM memories "
                f"WHERE memory_id IN ({placeholders})",
                memory_ids,
            ).fetchall()
            found = {row["memory_id"]: dict(row) for row in rows}
            scope = self.active_scope()
            active_ids = set(scope.get("memory_ids", []))
            if scope.get("library_id"):
                found = {memory_id: item for memory_id, item in found.items() if memory_id in active_ids}
            missing = next((memory_id for memory_id in memory_ids if memory_id not in found), None)
            if missing is not None:
                raise ValueError(f"memory_id not found: {missing}")
            return [found[memory_id] for memory_id in memory_ids]
        finally:
            connection.close()
