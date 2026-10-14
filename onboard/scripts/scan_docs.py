#!/usr/bin/env python3
"""Scan docs frontmatter for onboard facts."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

DATE_FORMAT = "%Y-%m-%d %H:%M"
EXCLUDED_BUSINESS_DOCS = {
    "docs/onboard.md",
    "docs/AGENTS.md",
    "docs/CLAUDE.md",
}


@dataclass(frozen=True)
class DocumentMeta:
    """Parsed frontmatter fields needed by onboard."""

    path: str
    description: str
    related: tuple[str, ...]
    sessions: tuple[str, ...]
    generated_at: str
    source_latest_session_at: str
    source_latest_doc: str


@dataclass(frozen=True)
class SessionEntry:
    """A valid session record from a business document."""

    at: datetime
    path: str
    description: str
    tool: str
    session_id: str
    summary: str
    raw: str


@dataclass(frozen=True)
class InvalidSession:
    """An ignored session record with the reason."""

    path: str
    raw: str
    reason: str


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""

    parser = argparse.ArgumentParser(
        description="Scan docs frontmatter and report onboard cache freshness.",
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=".",
        help="Project root that contains docs/.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=5,
        help="Number of recent business documents to print.",
    )
    parser.add_argument(
        "--show-invalid",
        action="store_true",
        help="Print ignored invalid session records in text output.",
    )
    return parser.parse_args()


def unquote_value(value: str) -> str:
    """Remove simple YAML quotes from a scalar value."""

    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def extract_frontmatter(text: str) -> list[str] | None:
    """R eturn frontmatter lines when the Markdown file has a YAML block."""

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return lines[1:index]
    return None


def parse_frontmatter(lines: list[str]) -> dict[str, Any]:
    """Parse the small YAML subset used by docs frontmatter."""

    data: dict[str, Any] = {}
    current_list_key = ""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line[:1].isspace():
            if current_list_key and stripped.startswith("- "):
                item = unquote_value(stripped[2:])
                data.setdefault(current_list_key, []).append(item)
            continue
        current_list_key = ""
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        value = raw_value.strip()
        if not value:
            data[key] = []
            current_list_key = key
            continue
        data[key] = unquote_value(value)
    return data


def tuple_field(data: dict[str, Any], key: str) -> tuple[str, ...]:
    """Read a frontmatter field as a tuple of strings."""

    value = data.get(key, ())
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    if isinstance(value, str) and value:
        return (value,)
    return ()


def string_field(data: dict[str, Any], key: str) -> str:
    """Read a frontmatter field as a string."""

    value = data.get(key, "")
    if isinstance(value, str):
        return value
    return ""


def load_document_meta(path: Path, root: Path) -> DocumentMeta | None:
    """Load the needed frontmatter fields from one Markdown document."""

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None
    frontmatter = extract_frontmatter(text)
    if frontmatter is None:
        return None
    data = parse_frontmatter(frontmatter)
    relative_path = path.relative_to(root).as_posix()
    return DocumentMeta(
        path=relative_path,
        description=string_field(data, "description"),
        related=tuple_field(data, "related"),
        sessions=tuple_field(data, "sessions"),
        generated_at=string_field(data, "generated_at"),
        source_latest_session_at=string_field(data, "source_latest_session_at"),
        source_latest_doc=string_field(data, "source_latest_doc"),
    )


def is_business_doc(meta: DocumentMeta) -> bool:
    """Return whether a document should count as a business document."""

    if meta.path in EXCLUDED_BUSINESS_DOCS:
        return False
    if Path(meta.path).name == "index.md":
        return False
    return True


def is_placeholder_session(parts: list[str], raw: str) -> bool:
    """Return whether a session record is an explicit placeholder."""

    if any(part.lower() == "current" for part in parts):
        return True
    if len(parts) >= 2 and parts[1].lower() in {"session_id", "current"}:
        return True
    if "<" in raw or ">" in raw or "${" in raw:
        return True
    date_part = parts[3] if len(parts) == 4 else raw
    return any(token in date_part for token in ("YYYY", "MM", "DD", "HH"))


def parse_session(meta: DocumentMeta, raw: str) -> SessionEntry | InvalidSession:
    """Parse one session record."""

    parts = [part.strip() for part in raw.split("|")]
    if len(parts) != 4:
        return InvalidSession(meta.path, raw, "not_four_pipe_fields")
    if is_placeholder_session(parts, raw):
        return InvalidSession(meta.path, raw, "placeholder")
    try:
        at = datetime.strptime(parts[3], DATE_FORMAT)
    except ValueError:
        return InvalidSession(meta.path, raw, "invalid_datetime")
    return SessionEntry(
        at=at,
        path=meta.path,
        description=meta.description,
        tool=parts[0],
        session_id=parts[1],
        summary=parts[2],
        raw=raw,
    )


def parse_datetime(value: str) -> datetime | None:
    """Parse onboard datetime values."""

    try:
        return datetime.strptime(value, DATE_FORMAT)
    except ValueError:
        return None


def cache_status(cache_meta: DocumentMeta | None, latest: SessionEntry | None) -> str:
    """Return cache freshness status."""

    if cache_meta is None:
        return "missing"
    if latest is None:
        return "unknown"
    cache_at = parse_datetime(cache_meta.source_latest_session_at)
    if cache_at is None:
        return "unknown"
    if latest.at > cache_at:
        return "stale"
    return "fresh"


def collect(root: Path) -> dict[str, Any]:
    """Collect onboard scan facts from a project root."""

    docs_dir = root / "docs"
    markdown_paths = sorted(docs_dir.rglob("*.md")) if docs_dir.exists() else []
    metas: list[DocumentMeta] = []
    valid_sessions: list[SessionEntry] = []
    invalid_sessions: list[InvalidSession] = []
    cache_meta: DocumentMeta | None = None

    for path in markdown_paths:
        meta = load_document_meta(path, root)
        if meta is None:
            continue
        metas.append(meta)
        if meta.path == "docs/onboard.md":
            cache_meta = meta
        for raw_session in meta.sessions:
            parsed = parse_session(meta, raw_session)
            if isinstance(parsed, SessionEntry):
                valid_sessions.append(parsed)
            else:
                invalid_sessions.append(parsed)

    latest_by_doc: dict[str, SessionEntry] = {}
    for session in valid_sessions:
        meta = next(item for item in metas if item.path == session.path)
        if not is_business_doc(meta):
            continue
        current = latest_by_doc.get(session.path)
        if current is None or session.at > current.at:
            latest_by_doc[session.path] = session

    recent_docs = sorted(
        latest_by_doc.values(),
        key=lambda item: (item.at, item.path),
        reverse=True,
    )
    latest = recent_docs[0] if recent_docs else None
    return {
        "root": root.as_posix(),
        "markdown_count": len(markdown_paths),
        "frontmatter_count": len(metas),
        "valid_session_entry_count": len(valid_sessions),
        "invalid_session_entry_count": len(invalid_sessions),
        "business_session_doc_count": len(latest_by_doc),
        "cache_meta": cache_meta,
        "cache_status": cache_status(cache_meta, latest),
        "latest": latest,
        "recent_docs": recent_docs,
        "invalid_sessions": invalid_sessions,
    }


def print_text(result: dict[str, Any], top: int, show_invalid: bool) -> None:
    """Print scan results as compact text."""

    cache_meta = result["cache_meta"]
    latest = result["latest"]
    print("DOC_SCAN")
    print(f"root: {result['root']}")
    print(f"markdown_count: {result['markdown_count']}")
    print(f"frontmatter_count: {result['frontmatter_count']}")
    print(f"business_session_doc_count: {result['business_session_doc_count']}")
    print(f"valid_session_entry_count: {result['valid_session_entry_count']}")
    print(f"invalid_session_entry_count: {result['invalid_session_entry_count']}")
    print(f"cache_status: {result['cache_status']}")
    if cache_meta is not None:
        print(f"cache_source_latest_session_at: {cache_meta.source_latest_session_at}")
        print(f"cache_source_latest_doc: {cache_meta.source_latest_doc}")
    else:
        print("cache_source_latest_session_at: ")
        print("cache_source_latest_doc: ")
    if latest is not None:
        print(f"latest_business_session_at: {latest.at.strftime(DATE_FORMAT)}")
        print(f"latest_business_doc: {latest.path}")
        print(f"latest_business_session: {latest.raw}")
    else:
        print("latest_business_session_at: ")
        print("latest_business_doc: ")
        print("latest_business_session: ")
    print()
    print("RECENT_BUSINESS_DOCS")
    for session in result["recent_docs"][:top]:
        at = session.at.strftime(DATE_FORMAT)
        print(f"- {at} | {session.path} | {session.description} | {session.raw}")
    if show_invalid:
        print()
        print("INVALID_SESSIONS")
        for session in result["invalid_sessions"]:
            print(f"- {session.path} | {session.reason} | {session.raw}")


def main() -> None:
    """Run the scan command."""

    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    result = collect(root)
    print_text(result, args.top, args.show_invalid)


if __name__ == "__main__":
    main()
