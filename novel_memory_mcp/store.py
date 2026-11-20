"""Local Markdown source and SQLite FTS5 index for novel-memory blocks."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .files import atomic_write, file_lock

MEMORY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
FRONT_MATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", re.DOTALL)
QUERY_SEPARATOR_RE = re.compile(r"[\s,，、;；|/]+")
REQUIRED_FIELDS = ("id", "date_range", "title")


class ConflictError(ValueError):
    """An existing record or its version conflicts with the requested write."""

    def __init__(self, message: str, memory_id: str | None = None):
        super().__init__(message)
        self.memory_id = memory_id


class ScopeError(ValueError):
    """An existing scope could not be validated; never fall back to other stories."""


def _single_line(value: str, field: str, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    if any(character in value for character in "\r\n\v\f\x00\x1c\x1d\x1e\x85\u2028\u2029"):
        raise ValueError(f"{field} must be a single line")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{field} must not be blank")
    return value


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
        return self._parse_block(text, path, hashlib.sha256(raw).hexdigest())

    def _parse_block(self, text: str, path: Path, digest: str = "") -> MemoryBlock:
        text = text.removeprefix("\ufeff")
        match = FRONT_MATTER_RE.match(text)
        if not match:
            raise ValueError(f"{path}: missing YAML front matter")
        metadata = self._parse_metadata(match.group(1), path)
        missing = [field for field in REQUIRED_FIELDS if not metadata.get(field)]
        if missing:
            raise ValueError(f"{path}: missing required metadata: {', '.join(missing)}")
        memory_id = metadata["id"]
        if not MEMORY_ID_RE.fullmatch(memory_id) or memory_id.casefold() == "readme":
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
            digest=digest or hashlib.sha256(text.encode("utf-8")).hexdigest(),
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
                if value[0] == '"':
                    try:
                        decoded = json.loads(value)
                    except json.JSONDecodeError:
                        decoded = value[1:-1]
                    if not isinstance(decoded, str):
                        raise ValueError(f"{path}: metadata values must be strings")
                    value = decoded
                else:
                    value = value[1:-1]
            value = _single_line(value, key, required=False)
            metadata[key] = value
        return metadata

    def _scan_directory(self, memory_dir: Path) -> tuple[list[MemoryBlock], list[dict[str, str]]]:
        if not memory_dir.is_dir():
            raise RuntimeError(f"memory directory does not exist or is not a directory: {memory_dir}")
        blocks: dict[str, MemoryBlock] = {}
        duplicate_ids: set[str] = set()
        errors: list[dict[str, str]] = []
        for path in sorted(memory_dir.rglob("*.md")):
            if path.resolve().is_relative_to((self.index_path.parent / "history").resolve()):
                continue
            try:
                if not path.resolve().is_relative_to(memory_dir.resolve()):
                    raise ValueError("memory file must stay inside its library")
                block = self._read_block(path)
            except (OSError, ValueError, TypeError) as exc:
                errors.append({"file": path.relative_to(memory_dir).as_posix(), "error": str(exc)})
                continue
            if block is None:
                continue
            if block.memory_id in blocks or block.memory_id in duplicate_ids:
                duplicate_ids.add(block.memory_id)
                errors.append({"file": path.relative_to(memory_dir).as_posix(), "memory_id": block.memory_id,
                               "error": f"duplicate memory id: {block.memory_id}"})
                blocks.pop(block.memory_id, None)
                continue
            blocks[block.memory_id] = block
        return list(blocks.values()), errors

    def _scan(self, scope: dict[str, object] | None = None) -> list[MemoryBlock]:
        scope = self.active_scope() if scope is None else scope
        blocks, _ = self._scan_directory(self._active_memory_dir(scope))
        if scope["library_id"]:
            selected = set(scope["memory_ids"])
            blocks = [block for block in blocks if block.memory_id in selected]
        return blocks

    def _library_block(self, library_id: str, memory_id: str) -> MemoryBlock:
        if not isinstance(library_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", library_id):
            raise ValueError("invalid library_id")
        if not isinstance(memory_id, str) or not MEMORY_ID_RE.fullmatch(memory_id):
            raise ValueError("invalid memory_id")
        blocks, errors = self._scan_directory(self.library_root / library_id / "memories")
        if any(error.get("memory_id") == memory_id for error in errors):
            raise ConflictError(f"duplicate memory id: {memory_id}; fix the duplicate files first")
        for block in blocks:
            if block.memory_id == memory_id:
                return block
        raise ValueError(f"memory not found: {memory_id}")

    def _backup(self, path: Path, library_id: str, memory_id: str) -> Path:
        with path.open("r", encoding="utf-8", newline="") as handle:
            raw = handle.read()
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        backup = self.index_path.parent / "history" / library_id / memory_id / f"{time.time_ns()}-{digest}.md"
        atomic_write(backup, raw)
        return backup

    def list_libraries(self) -> list[dict[str, object]]:
        root = self.library_root
        if not root.is_dir():
            return []
        result = []
        for directory in sorted(path for path in root.iterdir() if path.is_dir() and not path.name.startswith(".")):
            metadata_path = directory / "library.json"
            metadata: dict[str, object] = {}
            errors: list[dict[str, str]] = []
            if metadata_path.is_file():
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8").removeprefix("\ufeff"))
                    if not isinstance(metadata, dict):
                        raise TypeError("library metadata must be a JSON object")
                except (OSError, ValueError, TypeError) as exc:
                    metadata = {}
                    errors.append({"file": "library.json", "error": str(exc)})
            memory_dir = directory / "memories"
            count = len(list(memory_dir.rglob("*.md"))) if memory_dir.is_dir() else 0
            result.append({
                "library_id": directory.name,
                "name": str(metadata.get("name", directory.name)),
                "description": str(metadata.get("description", "")),
                "memory_count": count,
                "errors": errors,
            })
        return result

    def list_library_memories(
        self, library_id: str, errors: list[dict[str, str]] | None = None,
    ) -> list[dict[str, object]]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", library_id):
            raise ValueError("invalid library_id")
        directory = self.library_root / library_id / "memories"
        if not directory.is_dir():
            raise ValueError(f"library not found: {library_id}")
        blocks, issues = self._scan_directory(directory)
        if errors is not None:
            errors.extend(issues)
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
        block = self._library_block(library_id, memory_id)
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
        expected_digest: str | None = None,
    ) -> dict[str, object]:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", library_id):
            raise ValueError("invalid library_id")
        if not isinstance(memory_id, str) or not MEMORY_ID_RE.fullmatch(memory_id) or memory_id.casefold() == "readme":
            raise ValueError("invalid memory_id")
        date_range = _single_line(date_range, "date_range")
        title = _single_line(title, "title")
        if not isinstance(content, str):
            raise TypeError("content must be a string")
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
        if isinstance(tags, list):
            tags_str = ", ".join(_single_line(tag, "tags", required=False) for tag in tags)
        else:
            tags_str = _single_line(tags, "tags", required=False)

        markdown_text = (
            f"---\n"
            f"id: {memory_id}\n"
            f"date_range: {json.dumps(date_range, ensure_ascii=False)}\n"
            f"title: {json.dumps(title, ensure_ascii=False)}\n"
            f"tags: {json.dumps(tags_str, ensure_ascii=False)}\n"
            f"---\n\n"
            f"{content}\n"
        )
        # Lock by logical ID, including records whose actual filename differs.
        canonical = directory / f"{memory_id}.md"
        self._parse_block(markdown_text, canonical)
        with file_lock(canonical, self.index_path.parent / "locks"):
            try:
                old = self._library_block(library_id, memory_id)
            except ConflictError:
                raise
            except ValueError:
                old = None
            target = Path(old.path) if old else canonical
            exists = target.exists()
            if is_new and exists and not overwrite:
                raise ConflictError(f"memory already exists: {memory_id}", memory_id)
            if not is_new and old is None:
                raise ValueError(f"memory not found: {memory_id}")
            if expected_digest is not None and (old is None or old.digest != expected_digest):
                raise ConflictError("record changed since it was opened; reload or save a separate copy")
            if exists:
                self._backup(target, library_id, memory_id)
            atomic_write(target, markdown_text)
            return self.get_library_memory(library_id, memory_id)

    def import_raw_markdown(
        self,
        library_id: str,
        filename: str,
        raw_text: str,
        overwrite: bool = False,
        expected_digest: str | None = None,
    ) -> dict[str, object]:
        """Import an existing Markdown file directly, extracting front matter if present."""
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", library_id):
            raise ValueError("invalid library_id")
        directory = self.library_root / library_id / "memories"
        if not directory.is_dir():
            raise ValueError(f"library not found: {library_id}")

        raw_text = raw_text.removeprefix("\ufeff")
        stem = Path(filename).stem
        clean_stem = re.sub(r"[^A-Za-z0-9_-]", "_", stem).strip("_")
        suffix = hashlib.sha256(filename.encode("utf-8")).hexdigest()[:16]
        generated_id = stem if MEMORY_ID_RE.fullmatch(stem) else f"memory_{clean_stem[:70]}_{suffix}"

        match = FRONT_MATTER_RE.match(raw_text)
        if match:
            meta = self._parse_metadata(match.group(1), Path(filename))
            memory_id = meta.get("id", generated_id)
            date_range = meta.get("date_range", "未标注时间")
            title = meta.get("title", stem or "未命名阶段")
            tags = meta.get("tags", "")
            content = match.group(2).lstrip("\r\n").rstrip()
        else:
            memory_id = generated_id
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
            raise ValueError("invalid memory_id in imported metadata")

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
            expected_digest=expected_digest,
        )

    def delete_library_memory(
        self, library_id: str, memory_id: str, expected_digest: str | None = None,
    ) -> bool:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", library_id):
            raise ValueError("invalid library_id")
        if not MEMORY_ID_RE.fullmatch(memory_id):
            raise ValueError("invalid memory_id")
        canonical = self.library_root / library_id / "memories" / f"{memory_id}.md"
        with file_lock(canonical, self.index_path.parent / "locks"):
            block = self._library_block(library_id, memory_id)
            if expected_digest is not None and block.digest != expected_digest:
                raise ConflictError("record changed; reload before deleting")
            target = Path(block.path)
            # Validate scope before touching the record; keep a recoverable copy.
            with file_lock(self.scope_path, self.index_path.parent / "locks"):
                scope = self._scope()
                self._backup(target, library_id, memory_id)
                if scope["library_id"] == library_id and memory_id in scope["memory_ids"]:
                    remaining = [item for item in scope["memory_ids"] if item != memory_id]
                    atomic_write(self.scope_path, json.dumps(
                        {"library_id": library_id, "memory_ids": remaining}, ensure_ascii=False, indent=2,
                    ) + "\n")
                target.unlink()
        return True

    def create_library(self, library_id: str, name: str, description: str = "") -> dict[str, object]:
        name = name.strip()
        if not name:
            raise ValueError("name must not be blank")
        description = description.strip()

        if not library_id or not library_id.strip():
            library_id = f"novel_{uuid.uuid4().hex[:16]}"
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
        atomic_write(library_dir / "library.json",
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
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
        atomic_write(metadata_path,
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
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
        try:
            text = self.scope_path.read_text(encoding="utf-8").removeprefix("\ufeff")
        except FileNotFoundError:
            return {"library_id": "", "memory_ids": []}
        except (OSError, UnicodeError) as exc:
            raise ScopeError("active scope cannot be read; repair it in the management console") from exc
        try:
            value = json.loads(text)
            if not isinstance(value, dict):
                raise TypeError("scope must be an object")
            library_id = value.get("library_id", "")
            ids = value.get("memory_ids", [])
            if not isinstance(library_id, str) or (library_id and not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", library_id,
            )):
                raise ValueError("invalid library_id")
            if not isinstance(ids, list) or any(
                not isinstance(item, str) or not MEMORY_ID_RE.fullmatch(item) for item in ids
            ) or (not library_id and ids):
                raise ValueError("invalid memory_ids")
            return {"library_id": library_id, "memory_ids": list(dict.fromkeys(ids))}
        except (ValueError, TypeError) as exc:
            raise ScopeError("active scope is invalid; select and save a valid scope in the management console") from exc

    def active_scope(self) -> dict[str, object]:
        return self._scope()

    def set_scope(self, library_id: str, memory_ids: list[str]) -> dict[str, object]:
        if not isinstance(library_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", library_id):
            raise ValueError("invalid library_id")
        if not isinstance(memory_ids, list) or any(not isinstance(item, str) for item in memory_ids):
            raise ValueError("memory_ids must be a list of strings")
        library_dir = self.library_root / library_id
        memory_dir = library_dir / "memories"
        if not memory_dir.is_dir():
            raise ValueError(f"library not found: {library_id}")
        blocks, _ = self._scan_directory(memory_dir)
        available = {block.memory_id for block in blocks}
        if not set(memory_ids).issubset(available):
            raise ValueError("scope contains an unknown memory_id")
        value = {"library_id": library_id, "memory_ids": list(dict.fromkeys(memory_ids))}
        with file_lock(self.scope_path, self.index_path.parent / "locks"):
            atomic_write(self.scope_path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        return value

    def _active_memory_dir(self, scope: dict[str, object] | None = None) -> Path:
        scope = self.active_scope() if scope is None else scope
        library_id = str(scope.get("library_id", ""))
        if library_id:
            directory = self.library_root / library_id / "memories"
            if not directory.is_dir():
                raise ScopeError(f"active library does not exist: {library_id}")
            return directory
        return self.memory_dir

    @contextmanager
    def _indexed_snapshot(self) -> Iterator[tuple[sqlite3.Connection, dict[str, object]]]:
        scope = self.active_scope()
        connection = self._connect()
        try:
            # The shared rebuildable index must not change between sync and read.
            connection.execute("BEGIN IMMEDIATE")
            self._sync(connection, scope)
            yield connection, scope
            connection.commit()
        finally:
            connection.close()

    def _sync(self, connection: sqlite3.Connection, scope: dict[str, object]) -> None:
        blocks = self._scan(scope)
        current = {block.memory_id: block for block in blocks}
        existing = {
            row["memory_id"]: (row["digest"], row["path"])
            for row in connection.execute("SELECT memory_id, digest, path FROM memories")
        }
        for memory_id, old_identity in existing.items():
            if memory_id in current and old_identity == (current[memory_id].digest, current[memory_id].path):
                continue
            connection.execute("DELETE FROM memories WHERE memory_id = ?", (memory_id,))
            connection.execute("DELETE FROM memory_fts WHERE memory_id = ?", (memory_id,))
        for block in blocks:
            if existing.get(block.memory_id) == (block.digest, block.path):
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
        with self._indexed_snapshot() as (connection, scope):
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

    def list_tags(self) -> list[dict[str, str | list[str]]]:
        """Return compact discovery metadata for every maintained memory block."""
        with self._indexed_snapshot() as (connection, scope):
            rows = connection.execute(
                "SELECT memory_id, date_range, title, tags FROM memories ORDER BY memory_id"
            ).fetchall()
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

    def get(self, memory_id: str) -> dict[str, str]:
        if not MEMORY_ID_RE.fullmatch(memory_id):
            raise ValueError("memory_id must contain only letters, digits, underscores, or hyphens")
        with self._indexed_snapshot() as (connection, scope):
            row = connection.execute(
                "SELECT memory_id, date_range, title, content FROM memories WHERE memory_id = ?",
                (memory_id,),
            ).fetchone()
            active_ids = set(scope.get("memory_ids", []))
            if row is None or (scope.get("library_id") and memory_id not in active_ids):
                raise ValueError(f"memory_id not found: {memory_id}")
            return dict(row)

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

        with self._indexed_snapshot() as (connection, scope):
            # The public operation accepts exactly one or two IDs.
            statement = (
                "SELECT memory_id, date_range, title, content FROM memories WHERE memory_id = ?"
                if len(memory_ids) == 1 else
                "SELECT memory_id, date_range, title, content FROM memories WHERE memory_id IN (?, ?)"
            )
            rows = connection.execute(statement, memory_ids).fetchall()
            found = {row["memory_id"]: dict(row) for row in rows}
            active_ids = set(scope.get("memory_ids", []))
            if scope.get("library_id"):
                found = {memory_id: item for memory_id, item in found.items() if memory_id in active_ids}
            missing = next((memory_id for memory_id in memory_ids if memory_id not in found), None)
            if missing is not None:
                raise ValueError(f"memory_id not found: {missing}")
            return [found[memory_id] for memory_id in memory_ids]
