"""Conversation discovery, categorization, trash, and retention management."""

from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from .profiles import PROFILE_DATA_ROOT


RETENTION_DAYS = 14
DEFAULT_CATEGORIES = ("未分类", "工作", "学习", "开发", "临时", "保留")


class ConversationError(RuntimeError):
    """Raised when conversation state cannot be changed safely."""


@dataclass(slots=True)
class Conversation:
    provider: str
    session_id: str
    title: str
    cwd: str
    updated_at: float
    source_path: str
    category: str = "未分类"
    size_bytes: int = 0

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.session_id}"


@dataclass(slots=True)
class TrashEntry:
    provider: str
    session_id: str
    title: str
    cwd: str
    category: str
    trashed_at: float
    expires_at: float
    entry_dir: str

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.session_id}"


def _parse_time(value: Any, fallback: float = 0.0) -> float:
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000.0 if number > 10_000_000_000 else number
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return fallback


def _clean_title(value: Any, fallback: str) -> str:
    text = " ".join(str(value or "").strip().split())
    return (text or fallback)[:120]


def _tree_size(path: Path) -> int:
    try:
        if path.is_file() or path.is_symlink():
            return path.lstat().st_size
        return sum(
            item.lstat().st_size
            for item in path.rglob("*")
            if item.is_file() or item.is_symlink()
        )
    except OSError:
        return 0


def _jsonl_objects(path: Path) -> Iterable[dict[str, Any]]:
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                yield value


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    temporary.write_text(data, encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _remove_jsonl_records(
    path: Path,
    predicate: Callable[[dict[str, Any]], bool],
    stash: Path,
) -> int:
    if not path.is_file():
        stash.parent.mkdir(parents=True, exist_ok=True)
        stash.write_text("", encoding="utf-8")
        return 0
    kept: list[str] = []
    removed: list[str] = []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                kept.append(line)
                continue
            if isinstance(value, dict) and predicate(value):
                removed.append(line)
            else:
                kept.append(line)
    stash.parent.mkdir(parents=True, exist_ok=True)
    stash.write_text("".join(removed), encoding="utf-8")
    _atomic_write(path, "".join(kept))
    return len(removed)


def _merge_jsonl_records(path: Path, stash: Path, sort_field: str) -> None:
    if not stash.is_file() or not stash.stat().st_size:
        return
    lines: list[str] = []
    if path.is_file():
        lines.extend(path.read_text(encoding="utf-8", errors="replace").splitlines(True))
    lines.extend(stash.read_text(encoding="utf-8", errors="replace").splitlines(True))
    lines = list(dict.fromkeys(lines))

    def key(line: str) -> tuple[float, str]:
        try:
            value = json.loads(line)
            raw = value.get(sort_field) if isinstance(value, dict) else None
        except json.JSONDecodeError:
            raw = None
        return (_parse_time(raw, 0.0), line)

    _atomic_write(path, "".join(sorted(lines, key=key)))


def _encode_db_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"__bytes__": base64.b64encode(value).decode("ascii")}
    return value


def _decode_db_value(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {"__bytes__"}:
        return base64.b64decode(value["__bytes__"])
    return value


def _sqlite_related_rows(database: Path, thread_id: str) -> dict[str, dict[str, Any]]:
    if not database.is_file():
        return {}
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    result: dict[str, dict[str, Any]] = {}
    try:
        tables = [
            row[0]
            for row in connection.execute(
                "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
            )
        ]
        for table in tables:
            columns = [row[1] for row in connection.execute(f'pragma table_info("{table}")')]
            match_columns = [
                column
                for column in columns
                if column in {"thread_id", "parent_thread_id", "child_thread_id"}
            ]
            if table == "threads" and "id" in columns:
                match_columns.append("id")
            if not match_columns:
                continue
            where = " or ".join(f'"{column}" = ?' for column in match_columns)
            rows = connection.execute(
                f'select * from "{table}" where {where}',
                [thread_id] * len(match_columns),
            ).fetchall()
            if rows:
                result[table] = {
                    "columns": columns,
                    "rows": [
                        [_encode_db_value(row[column]) for column in columns]
                        for row in rows
                    ],
                }
    finally:
        connection.close()
    return result


def _sqlite_delete_related(database: Path, thread_id: str) -> None:
    if not database.is_file():
        return
    connection = sqlite3.connect(database, timeout=2.0)
    try:
        connection.execute("pragma busy_timeout=2000")
        connection.execute("begin immediate")
        tables = [
            row[0]
            for row in connection.execute(
                "select name from sqlite_master where type='table' and name not like 'sqlite_%'"
            )
        ]
        ordered = [table for table in tables if table != "threads"]
        if "threads" in tables:
            ordered.append("threads")
        for table in ordered:
            columns = [row[1] for row in connection.execute(f'pragma table_info("{table}")')]
            match_columns = [
                column
                for column in columns
                if column in {"thread_id", "parent_thread_id", "child_thread_id"}
            ]
            if table == "threads" and "id" in columns:
                match_columns.append("id")
            if not match_columns:
                continue
            where = " or ".join(f'"{column}" = ?' for column in match_columns)
            connection.execute(
                f'delete from "{table}" where {where}',
                [thread_id] * len(match_columns),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _sqlite_restore_rows(database: Path, snapshot: dict[str, Any]) -> None:
    if not snapshot or not database.is_file():
        return
    connection = sqlite3.connect(database, timeout=2.0)
    try:
        connection.execute("pragma busy_timeout=2000")
        connection.execute("begin immediate")
        names = list(snapshot)
        ordered = (["threads"] if "threads" in names else []) + [
            table for table in names if table != "threads"
        ]
        for table in ordered:
            state = snapshot[table]
            columns = list(state.get("columns") or [])
            rows = list(state.get("rows") or [])
            if not columns or not rows:
                continue
            quoted = ",".join(f'"{column}"' for column in columns)
            placeholders = ",".join("?" for _ in columns)
            sql = f'insert or ignore into "{table}" ({quoted}) values ({placeholders})'
            for row in rows:
                connection.execute(sql, [_decode_db_value(value) for value in row])
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def provider_is_running(provider: str) -> bool:
    expected = {"claude": "claude", "codex": "codex"}.get(provider)
    if expected is None:
        return False
    proc = Path("/proc")
    if not proc.is_dir():
        return False
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if (entry / "comm").read_text(encoding="utf-8").strip() == expected:
                return True
            raw = (entry / "cmdline").read_bytes().split(b"\0")
            if raw and Path(raw[0].decode("utf-8", errors="ignore")).name == expected:
                return True
        except (OSError, UnicodeError):
            continue
    return False


class ConversationManager:
    def __init__(
        self,
        profile_id: str = "strict-auto-ip",
        *,
        profile_data_root: Path = PROFILE_DATA_ROOT,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.profile_id = profile_id
        self.profile_root = Path(profile_data_root) / profile_id
        self.home = self.profile_root / "home"
        self.state_root = self.profile_root / "conversation-manager"
        self.metadata_path = self.state_root / "metadata.json"
        self.trash_root = self.state_root / "trash"
        self._now = now
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.trash_root.mkdir(parents=True, exist_ok=True)

    def _metadata(self) -> dict[str, Any]:
        if self.metadata_path.is_file():
            try:
                value = json.loads(self.metadata_path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    categories = value.get("categories")
                    assignments = value.get("assignments")
                    if isinstance(categories, list) and isinstance(assignments, dict):
                        return value
            except (OSError, json.JSONDecodeError):
                pass
        return {
            "version": 1,
            "categories": list(DEFAULT_CATEGORIES),
            "assignments": {},
        }

    def _save_metadata(self, metadata: dict[str, Any]) -> None:
        _atomic_write(
            self.metadata_path,
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        )

    def categories(self) -> list[str]:
        metadata = self._metadata()
        categories = [str(item) for item in metadata.get("categories", []) if str(item).strip()]
        if "未分类" not in categories:
            categories.insert(0, "未分类")
        return categories

    def add_category(self, name: str) -> str:
        name = " ".join(name.strip().split())
        if not name:
            raise ConversationError("分类名称不能为空")
        if len(name) > 32:
            raise ConversationError("分类名称不能超过 32 个字符")
        metadata = self._metadata()
        categories = metadata.setdefault("categories", list(DEFAULT_CATEGORIES))
        if name not in categories:
            categories.append(name)
            self._save_metadata(metadata)
        return name

    def remove_category(self, name: str) -> None:
        if name == "未分类":
            raise ConversationError("“未分类”不能删除")
        metadata = self._metadata()
        metadata["categories"] = [item for item in metadata.get("categories", []) if item != name]
        assignments = metadata.setdefault("assignments", {})
        for key, value in list(assignments.items()):
            if value == name:
                assignments[key] = "未分类"
        self._save_metadata(metadata)

    def assign_category(self, key: str, category: str) -> None:
        if category not in self.categories():
            raise ConversationError(f"未知分类：{category}")
        metadata = self._metadata()
        metadata.setdefault("assignments", {})[key] = category
        self._save_metadata(metadata)

    def _category_for(self, provider: str, session_id: str, metadata: dict[str, Any]) -> str:
        value = str(metadata.get("assignments", {}).get(f"{provider}:{session_id}", "未分类"))
        return value if value in self.categories() else "未分类"

    def discover(self) -> list[Conversation]:
        metadata = self._metadata()
        conversations = [*self._discover_claude(metadata), *self._discover_codex(metadata)]
        return sorted(conversations, key=lambda item: item.updated_at, reverse=True)

    def _discover_claude(self, metadata: dict[str, Any]) -> list[Conversation]:
        root = self.home / ".claude"
        projects = root / "projects"
        if not projects.is_dir():
            return []
        result: list[Conversation] = []
        for project in sorted(path for path in projects.iterdir() if path.is_dir()):
            # Claude subagents live below <session-id>/subagents; only direct
            # JSONL children are top-level resumable conversations.
            for path in sorted(project.glob("*.jsonl")):
                session_id = path.stem
                cwd = ""
                updated = path.stat().st_mtime
                title = ""
                fallback_prompt = ""
                for value in _jsonl_objects(path):
                    session_id = str(value.get("sessionId") or session_id)
                    cwd = str(value.get("cwd") or cwd)
                    updated = max(
                        updated,
                        _parse_time(value.get("timestamp"), updated),
                        _parse_time(value.get("startTime"), updated),
                    )
                    if value.get("type") == "ai-title" and value.get("aiTitle"):
                        title = str(value["aiTitle"])
                    elif value.get("type") == "last-prompt" and value.get("lastPrompt"):
                        fallback_prompt = str(value["lastPrompt"])
                title = _clean_title(title or fallback_prompt, f"Claude 会话 {session_id[:8]}")
                related = [
                    path,
                    project / session_id,
                    root / "file-history" / session_id,
                    root / "session-env" / session_id,
                ]
                result.append(
                    Conversation(
                        provider="claude",
                        session_id=session_id,
                        title=title,
                        cwd=cwd,
                        updated_at=updated,
                        source_path=str(path),
                        category=self._category_for("claude", session_id, metadata),
                        size_bytes=sum(_tree_size(item) for item in related if item.exists()),
                    )
                )
        return result

    def _codex_thread_metadata(self) -> dict[str, dict[str, Any]]:
        database = self.home / ".codex" / "state_5.sqlite"
        if not database.is_file():
            return {}
        result: dict[str, dict[str, Any]] = {}
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=1.0)
            connection.row_factory = sqlite3.Row
            columns = {row[1] for row in connection.execute('pragma table_info("threads")')}
            wanted = [
                item
                for item in (
                    "id",
                    "title",
                    "first_user_message",
                    "preview",
                    "name",
                    "cwd",
                    "updated_at_ms",
                    "updated_at",
                )
                if item in columns
            ]
            if "id" not in wanted:
                connection.close()
                return {}
            sql = "select " + ",".join(f'"{item}"' for item in wanted) + ' from "threads"'
            for row in connection.execute(sql):
                result[str(row["id"])] = dict(row)
            connection.close()
        except sqlite3.Error:
            return {}
        return result

    def _discover_codex(self, metadata: dict[str, Any]) -> list[Conversation]:
        root = self.home / ".codex"
        sessions = root / "sessions"
        if not sessions.is_dir():
            return []
        thread_metadata = self._codex_thread_metadata()
        result: list[Conversation] = []
        for path in sorted(sessions.rglob("*.jsonl")):
            session_id = ""
            cwd = ""
            updated = path.stat().st_mtime
            for value in _jsonl_objects(path):
                updated = max(updated, _parse_time(value.get("timestamp"), updated))
                if value.get("type") != "session_meta":
                    continue
                payload = value.get("payload")
                if isinstance(payload, dict):
                    session_id = str(payload.get("id") or payload.get("session_id") or "")
                    cwd = str(payload.get("cwd") or cwd)
                    updated = max(updated, _parse_time(payload.get("timestamp"), updated))
                break
            if not session_id:
                continue
            state = thread_metadata.get(session_id, {})
            cwd = str(state.get("cwd") or cwd)
            if state.get("updated_at_ms") is not None:
                updated = max(updated, _parse_time(state.get("updated_at_ms"), updated))
            elif state.get("updated_at") is not None:
                updated = max(updated, _parse_time(state.get("updated_at"), updated))
            title = next(
                (
                    str(state.get(field))
                    for field in ("title", "first_user_message", "name", "preview")
                    if state.get(field)
                ),
                "",
            )
            title = _clean_title(title, f"Codex 会话 {session_id[:8]}")
            related = [path, *sorted((root / "shell_snapshots").glob(f"{session_id}.*"))]
            result.append(
                Conversation(
                    provider="codex",
                    session_id=session_id,
                    title=title,
                    cwd=cwd,
                    updated_at=updated,
                    source_path=str(path),
                    category=self._category_for("codex", session_id, metadata),
                    size_bytes=sum(_tree_size(item) for item in related if item.exists()),
                )
            )
        return result

    def trash_entries(self) -> list[TrashEntry]:
        entries: list[TrashEntry] = []
        for manifest in self.trash_root.glob("*/*/manifest.json"):
            try:
                value = json.loads(manifest.read_text(encoding="utf-8"))
                trashed_at = float(value["trashed_at"])
                entries.append(
                    TrashEntry(
                        provider=str(value["provider"]),
                        session_id=str(value["session_id"]),
                        title=str(value.get("title") or value["session_id"]),
                        cwd=str(value.get("cwd") or ""),
                        category=str(value.get("category") or "未分类"),
                        trashed_at=trashed_at,
                        expires_at=trashed_at + RETENTION_DAYS * 86400,
                        entry_dir=str(manifest.parent),
                    )
                )
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
        return sorted(entries, key=lambda item: item.trashed_at, reverse=True)

    def _active_by_key(self, key: str) -> Conversation:
        for conversation in self.discover():
            if conversation.key == key:
                return conversation
        raise ConversationError("找不到该会话，可能已经被移动或删除")

    def _move_payload(
        self, provider_root: Path, path: Path, entry: Path
    ) -> dict[str, str] | None:
        if not path.exists():
            return None
        relative = path.relative_to(provider_root)
        target = entry / "payload" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        kind = "dir" if path.is_dir() else "file"
        shutil.move(str(path), str(target))
        return {"relative": str(relative), "kind": kind}

    @staticmethod
    def _rollback_payload(provider_root: Path, entry: Path, moved: list[dict[str, str]]) -> None:
        for item in reversed(moved):
            relative = Path(item["relative"])
            source = entry / "payload" / relative
            target = provider_root / relative
            if not source.exists() or target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))

    def move_to_trash(self, key: str) -> TrashEntry:
        conversation = self._active_by_key(key)
        if provider_is_running(conversation.provider):
            label = "Claude Code" if conversation.provider == "claude" else "Codex"
            raise ConversationError(f"请先退出正在运行的 {label}，再移动其会话")
        destination = self.trash_root / conversation.provider / conversation.session_id
        if destination.exists():
            raise ConversationError("该会话已经在回收站中")
        temporary = destination.with_name(destination.name + f".pending-{os.getpid()}")
        temporary.mkdir(parents=True, exist_ok=False)
        try:
            if conversation.provider == "claude":
                moved, index_files, sqlite_snapshots = self._trash_claude(conversation, temporary)
            elif conversation.provider == "codex":
                moved, index_files, sqlite_snapshots = self._trash_codex(conversation, temporary)
            else:
                raise ConversationError(f"未知 provider：{conversation.provider}")
            trashed_at = self._now()
            manifest = {
                "version": 1,
                "provider": conversation.provider,
                "session_id": conversation.session_id,
                "title": conversation.title,
                "cwd": conversation.cwd,
                "category": conversation.category,
                "trashed_at": trashed_at,
                "original_source_path": conversation.source_path,
                "moved": moved,
                "index_files": index_files,
                "sqlite_snapshots": sqlite_snapshots,
            }
            (temporary / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, destination)
            return TrashEntry(
                provider=conversation.provider,
                session_id=conversation.session_id,
                title=conversation.title,
                cwd=conversation.cwd,
                category=conversation.category,
                trashed_at=trashed_at,
                expires_at=trashed_at + RETENTION_DAYS * 86400,
                entry_dir=str(destination),
            )
        except ConversationError:
            raise
        except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
            raise ConversationError(f"移动会话失败：{exc}") from exc

    def _trash_claude(
        self, conversation: Conversation, entry: Path
    ) -> tuple[list[dict[str, str]], dict[str, str], dict[str, Any]]:
        root = self.home / ".claude"
        source = Path(conversation.source_path)
        project = source.parent
        candidates = [
            source,
            project / conversation.session_id,
            root / "file-history" / conversation.session_id,
            root / "session-env" / conversation.session_id,
        ]
        moved: list[dict[str, str]] = []
        stash = entry / "indexes" / "history.jsonl"
        try:
            for path in candidates:
                state = self._move_payload(root, path, entry)
                if state is not None:
                    moved.append(state)
            _remove_jsonl_records(
                root / "history.jsonl",
                lambda value: str(value.get("sessionId") or "") == conversation.session_id,
                stash,
            )
            return moved, {"history": "indexes/history.jsonl"}, {}
        except Exception:
            _merge_jsonl_records(root / "history.jsonl", stash, "timestamp")
            self._rollback_payload(root, entry, moved)
            raise

    def restore(self, key: str) -> None:
        entry = next((item for item in self.trash_entries() if item.key == key), None)
        if entry is None:
            raise ConversationError("回收站中找不到该会话")
        if provider_is_running(entry.provider):
            label = "Claude Code" if entry.provider == "claude" else "Codex"
            raise ConversationError(f"请先退出正在运行的 {label}，再恢复其会话")
        directory = Path(entry.entry_dir)
        try:
            manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConversationError(f"回收站清单损坏：{exc}") from exc
        provider_root = self.home / (".claude" if entry.provider == "claude" else ".codex")
        for item in manifest.get("moved", []):
            relative = Path(str(item["relative"]))
            source = directory / "payload" / relative
            target = provider_root / relative
            if target.exists():
                raise ConversationError(f"恢复目标已经存在：{target}")
            if source.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))

        try:
            if entry.provider == "claude":
                _merge_jsonl_records(
                    provider_root / "history.jsonl",
                    directory / "indexes" / "history.jsonl",
                    "timestamp",
                )
            else:
                _merge_jsonl_records(
                    provider_root / "history.jsonl",
                    directory / "indexes" / "history.jsonl",
                    "ts",
                )
                _merge_jsonl_records(
                    provider_root / "session_index.jsonl",
                    directory / "indexes" / "session_index.jsonl",
                    "updated_at",
                )
                for name, snapshot in manifest.get("sqlite_snapshots", {}).items():
                    _sqlite_restore_rows(provider_root / name, snapshot)
        except (OSError, sqlite3.Error) as exc:
            raise ConversationError(f"恢复索引失败：{exc}") from exc

        shutil.rmtree(directory)

    def delete_permanently(self, key: str) -> None:
        entry = next((item for item in self.trash_entries() if item.key == key), None)
        if entry is None:
            raise ConversationError("回收站中找不到该会话")
        shutil.rmtree(entry.entry_dir)
        metadata = self._metadata()
        metadata.setdefault("assignments", {}).pop(key, None)
        self._save_metadata(metadata)

    def move_category_to_trash(self, category: str) -> tuple[int, list[str]]:
        targets = [item for item in self.discover() if item.category == category]
        moved = 0
        errors: list[str] = []
        for conversation in targets:
            try:
                self.move_to_trash(conversation.key)
                moved += 1
            except ConversationError as exc:
                errors.append(f"{conversation.title}: {exc}")
        return moved, errors

    def cleanup_expired(self) -> int:
        now = self._now()
        expired = [entry for entry in self.trash_entries() if entry.expires_at <= now]
        if not expired:
            return 0
        metadata = self._metadata()
        assignments = metadata.setdefault("assignments", {})
        for entry in expired:
            shutil.rmtree(entry.entry_dir, ignore_errors=True)
            assignments.pop(entry.key, None)
        self._save_metadata(metadata)
        return len(expired)

    def export_state(self) -> dict[str, Any]:
        """Return diagnostics without conversation bodies."""
        return {
            "profile_id": self.profile_id,
            "categories": self.categories(),
            "conversations": [asdict(item) for item in self.discover()],
            "trash": [asdict(item) for item in self.trash_entries()],
            "retention_days": RETENTION_DAYS,
        }

    def _trash_codex(
        self, conversation: Conversation, entry: Path
    ) -> tuple[list[dict[str, str]], dict[str, str], dict[str, Any]]:
        root = self.home / ".codex"
        source = Path(conversation.source_path)
        candidates = [source, *sorted((root / "shell_snapshots").glob(f"{conversation.session_id}.*"))]
        history_stash = entry / "indexes" / "history.jsonl"
        index_stash = entry / "indexes" / "session_index.jsonl"
        snapshots: dict[str, Any] = {}
        for name in ("state_5.sqlite", "thread_history_1.sqlite"):
            rows = _sqlite_related_rows(root / name, conversation.session_id)
            if rows:
                snapshots[name] = rows
        moved: list[dict[str, str]] = []
        deleted_databases: list[str] = []
        try:
            for path in candidates:
                state = self._move_payload(root, path, entry)
                if state is not None:
                    moved.append(state)
            _remove_jsonl_records(
                root / "history.jsonl",
                lambda value: str(value.get("session_id") or "") == conversation.session_id,
                history_stash,
            )
            _remove_jsonl_records(
                root / "session_index.jsonl",
                lambda value: str(value.get("id") or "") == conversation.session_id,
                index_stash,
            )
            for name in ("state_5.sqlite", "thread_history_1.sqlite"):
                _sqlite_delete_related(root / name, conversation.session_id)
                deleted_databases.append(name)
            return (
                moved,
                {"history": "indexes/history.jsonl", "session_index": "indexes/session_index.jsonl"},
                snapshots,
            )
        except Exception:
            _merge_jsonl_records(root / "history.jsonl", history_stash, "ts")
            _merge_jsonl_records(root / "session_index.jsonl", index_stash, "updated_at")
            for name in deleted_databases:
                _sqlite_restore_rows(root / name, snapshots.get(name, {}))
            self._rollback_payload(root, entry, moved)
            raise
