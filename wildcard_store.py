"""
Wildcard files as files: list, read, save, rename and delete them for editors.

Wildcards are edited a whole file at a time (a ``.yaml`` defines several
wildcard names, so editing by name would not round-trip). Each file is
addressed by its path under the wildcards folder, extension included
(``clothing.yaml``, ``pose/arms.txt``).

Every read returns a ``version`` (a hash of the bytes on disk). Changing an
existing file needs that version as ``base``; if the file changed meanwhile
the call fails with :class:`Conflict`, so two editors never overwrite each
other silently. Creating a file passes no ``base`` and fails if it exists.

The previous content is kept as ``<file>.bak`` (one generation) on save,
rename and delete. ``.bak`` is not a wildcard extension, so it is never loaded.

Line endings and a UTF-8 BOM are kept as the file had them; text going in and
out of this module always uses ``\\n``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from . import parser

MAX_FILE_CHARS = 1_000_000
_PART_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
_BOM = b"\xef\xbb\xbf"


class StoreError(ValueError):
    """Bad request: invalid path, unparsable content and the like."""


class NotFound(StoreError):
    pass


class Conflict(StoreError):
    """The file is not in the state the caller expected (changed, or already exists)."""

    def __init__(self, message: str, current: str | None):
        super().__init__(message)
        self.current = current  # version on disk now, or None if the file is gone


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def resolve(root: Path, rel: Any) -> tuple[str, Path]:
    """``(normalized rel, absolute path)`` for a wildcard file path, or StoreError."""
    if not isinstance(rel, str) or not rel or len(rel) > 255:
        raise StoreError("invalid path")
    parts = rel.replace("\\", "/").split("/")
    if not all(_PART_RE.match(p) for p in parts):
        raise StoreError("invalid path")
    if Path(parts[-1]).suffix.lower() not in parser.WILDCARD_EXTS:
        raise StoreError(f"extension must be one of {', '.join(parser.WILDCARD_EXTS)}")
    norm = "/".join(parts)
    path = root.joinpath(*parts)
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        raise StoreError("invalid path") from None
    return norm, path


# ---------------------------------------------------------------------------
# Content
# ---------------------------------------------------------------------------

def _version(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:16]


def _decode(raw: bytes) -> str:
    if raw.startswith(_BOM):
        raw = raw[len(_BOM):]
    return raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")


def _encode(text: str, like: bytes | None) -> bytes:
    """*text* (``\\n`` line ends) in the line-ending style and BOM of *like*."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if like is not None and b"\r\n" in like:
        text = text.replace("\n", "\r\n")
    raw = text.encode("utf-8")
    if like is not None and like.startswith(_BOM):
        raw = _BOM + raw
    return raw


def entries(rel: str, text: str) -> list[dict[str, Any]]:
    """
    Wildcard names a file defines, with value counts, named the way
    :func:`parser.find_wildcard` resolves them. Raises StoreError when a
    YAML/JSON file does not parse.
    """
    stem = rel.rpartition(".")[0]
    ext = "." + rel.rpartition(".")[2].lower()
    if ext == ".txt":
        return [{"name": stem, "count": len(parser._load_txt(text))}]
    if ext != ".json" and not parser._HAS_YAML:
        raise StoreError("PyYAML is not installed; YAML wildcards cannot be checked")
    try:
        data = parser._load_structured(text, ext)
    except json.JSONDecodeError as e:
        raise StoreError(f"JSON: {e.msg} (line {e.lineno}, column {e.colno})") from None
    except Exception as e:  # yaml.YAMLError carries its own position
        raise StoreError(f"YAML: {e}") from None
    if data is None and not text.strip():
        return []
    if isinstance(data, list):
        return [{"name": stem, "count": len(parser._list_values(data))}]
    if isinstance(data, dict):
        folder = stem.rpartition("/")[0]
        found = parser._structured_entries(data, folder)
        return [{"name": k, "count": len(v)} for k, v in found.items()]
    raise StoreError("the top level must be a list or a mapping")


def _check(rel: str, text: Any) -> list[dict[str, Any]]:
    if not isinstance(text, str) or len(text) > MAX_FILE_CHARS:
        raise StoreError(f"text must be a string up to {MAX_FILE_CHARS} chars")
    return entries(rel, text)


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def _read_raw(path: Path) -> bytes | None:
    try:
        return path.read_bytes() if path.is_file() else None
    except OSError:
        return None


def _expect(path: Path, base: Any) -> bytes:
    """Bytes of an existing file whose version is *base*, else NotFound/Conflict."""
    raw = _read_raw(path)
    if raw is None:
        raise NotFound("no such wildcard file")
    if not isinstance(base, str) or base != _version(raw):
        raise Conflict("the file changed since it was read", _version(raw))
    return raw


def _write_atomic(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(raw)
    os.replace(tmp, path)


def _backup(path: Path) -> None:
    shutil.copy2(path, path.with_name(path.name + ".bak"))


def _changed() -> None:
    parser._index_cache.clear()  # make expansion see the edit at once, not after the 2 s TTL


def list_files(root: Path) -> list[dict[str, Any]]:
    out = []
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*"), key=lambda p: p.as_posix().lower()):
        if not p.is_file() or p.suffix.lower() not in parser.WILDCARD_EXTS:
            continue
        rel = p.relative_to(root).as_posix()
        item: dict[str, Any] = {"path": rel, "size": p.stat().st_size}
        raw = _read_raw(p)
        try:
            item["version"] = _version(raw or b"")
            item["entries"] = entries(rel, _decode(raw or b""))
        except (StoreError, UnicodeDecodeError) as e:
            item["entries"], item["error"] = [], str(e)
        out.append(item)
    return out


def read_file(root: Path, rel: Any) -> dict[str, Any]:
    rel, path = resolve(root, rel)
    raw = _read_raw(path)
    if raw is None:
        raise NotFound("no such wildcard file")
    try:
        text = _decode(raw)
    except UnicodeDecodeError:
        raise StoreError("the file is not UTF-8") from None
    item: dict[str, Any] = {"path": rel, "text": text, "version": _version(raw)}
    try:
        item["entries"] = entries(rel, text)
    except StoreError as e:
        item["entries"], item["error"] = [], str(e)
    return item


def save_file(root: Path, rel: Any, text: Any, base: Any = None) -> dict[str, Any]:
    """Create (``base`` None) or overwrite (``base`` = version read) a file."""
    rel, path = resolve(root, rel)
    found = _check(rel, text)
    if base is None:
        old = _read_raw(path)
        if old is not None:
            raise Conflict("the file already exists", _version(old))
    else:
        old = _expect(path, base)
        _backup(path)
    raw = _encode(text, old)
    _write_atomic(path, raw)
    _changed()
    return {"path": rel, "version": _version(raw), "entries": found}


def rename_file(root: Path, rel: Any, to: Any, base: Any) -> dict[str, Any]:
    rel, path = resolve(root, rel)
    to, dest = resolve(root, to)
    raw = _expect(path, base)
    if dest.exists() and dest.resolve() != path.resolve():
        raise Conflict("a file with the new name already exists", None)
    found = entries(to, _decode(raw))  # e.g. .txt → .yaml must still parse
    _backup(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(path, dest)
    _changed()
    return {"path": to, "version": _version(raw), "entries": found}


def delete_file(root: Path, rel: Any, base: Any) -> dict[str, Any]:
    rel, path = resolve(root, rel)
    _expect(path, base)
    os.replace(path, path.with_name(path.name + ".bak"))
    _changed()
    return {"path": rel}
