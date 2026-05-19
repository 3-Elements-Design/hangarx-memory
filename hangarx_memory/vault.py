"""Obsidian vault integration for the cortex-memory plugin.

Pure stdlib — no `obsidian-tools`, no PyYAML, no ripgrep dependency. The
vault becomes a local mirror in front of Cortex: Hermes writes
conversation turns and memory writes as markdown notes with YAML
frontmatter, and reads them back during prefetch and via explicit
``vault_*`` tools.

The module exposes a single ``Vault`` class. All file writes go through
``_safe_path`` which enforces that the resolved path stays inside the
configured vault root, so a misbehaving model can't ``../../`` its way
out.
"""

from __future__ import annotations

import datetime as _dt
import io
import json
import logging
import re
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Frontmatter helpers (tiny YAML subset — strings, ints, bools, lists)
# ---------------------------------------------------------------------------

_FRONTMATTER_FENCE = "---"


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Return ``(frontmatter_dict, body)``.

    Supports a deliberately small YAML subset: ``key: value`` pairs,
    inline lists ``[a, b]`` and block lists ``- item``. Unknown
    structures are returned as raw strings so we never crash on a note
    written by a human or another tool.
    """
    if not text.startswith(_FRONTMATTER_FENCE + "\n") and not text.startswith(_FRONTMATTER_FENCE + "\r\n"):
        return {}, text

    lines = text.splitlines(keepends=False)
    if not lines:
        return {}, text

    # Find the closing fence.
    end_index = -1
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == _FRONTMATTER_FENCE:
            end_index = i
            break
    if end_index == -1:
        return {}, text

    fm_lines = lines[1:end_index]
    body = "\n".join(lines[end_index + 1:])

    data: dict[str, Any] = {}
    current_list: list[Any] | None = None
    for raw in fm_lines:
        if not raw.strip():
            current_list = None
            continue
        if current_list is not None and raw.lstrip().startswith("-"):
            item = raw.lstrip()[1:].strip()
            current_list.append(_coerce_scalar(item))
            continue

        if ":" not in raw:
            continue
        key, _, value = raw.partition(":")
        key = key.strip()
        value = value.strip()
        current_list = None
        if not value:
            # Could be a block list following.
            data[key] = []
            current_list = data[key]  # type: ignore[assignment]
            continue
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            data[key] = [_coerce_scalar(p.strip()) for p in inner.split(",") if p.strip()]
            continue
        data[key] = _coerce_scalar(value)

    return data, body


def dump_frontmatter(data: dict[str, Any]) -> str:
    """Serialize ``data`` to a YAML-ish frontmatter block (no trailing newline)."""
    if not data:
        return ""
    buf = io.StringIO()
    buf.write(_FRONTMATTER_FENCE + "\n")
    for key in sorted(data.keys()):
        value = data[key]
        if isinstance(value, list):
            if not value:
                buf.write(f"{key}: []\n")
                continue
            buf.write(f"{key}:\n")
            for item in value:
                buf.write(f"  - {_format_scalar(item)}\n")
            continue
        buf.write(f"{key}: {_format_scalar(value)}\n")
    buf.write(_FRONTMATTER_FENCE + "\n")
    return buf.getvalue()


def _coerce_scalar(text: str) -> Any:
    s = text.strip()
    if not s:
        return ""
    if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
        return s[1:-1]
    if s.lower() in {"true", "false"}:
        return s.lower() == "true"
    if s.lower() in {"null", "~"}:
        return None
    try:
        if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
            return int(s)
    except ValueError:
        pass
    return s


def _format_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if any(ch in text for ch in ":#\n") or text != text.strip():
        return json.dumps(text)
    return text


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------

_SLUG_PATTERN = re.compile(r"[^a-zA-Z0-9._-]+")


def slugify(text: str, *, max_length: int = 80) -> str:
    """Return a filesystem-safe slug for use as a note filename."""
    if not text:
        return "untitled"
    slug = _SLUG_PATTERN.sub("-", text.strip())
    slug = slug.strip("-._")
    if not slug:
        slug = "untitled"
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip("-._")
    return slug


# ---------------------------------------------------------------------------
# Vault facade
# ---------------------------------------------------------------------------


@dataclass
class VaultConfig:
    path: Path
    sessions_folder: str = "Hermes Sessions"
    link_style: str = "wikilink"  # or "markdown"
    search_enabled: bool = True
    auto_ingest: bool = False
    sync_mode: str = "per-session"  # off | per-session | daily | per-turn


class Vault:
    """Bounded helper for reading and writing inside an Obsidian vault."""

    def __init__(self, config: VaultConfig) -> None:
        self.config = config
        self.root = config.path.expanduser().resolve()
        self._write_lock = threading.Lock()

    # -- Path safety ---------------------------------------------------------

    def is_ready(self) -> bool:
        return self.root.is_dir()

    def _safe_path(self, relative: str) -> Path:
        """Resolve a vault-relative path and refuse traversal outside the root."""
        if not relative:
            raise ValueError("vault path is empty")
        rel = relative.strip()
        # Strip a leading slash so callers can pass either form.
        rel = rel.lstrip("/\\")
        # Strip a wrapping wikilink if a model handed one back.
        if rel.startswith("[[") and rel.endswith("]]"):
            rel = rel[2:-2]
        # Drop any inline alias `Note|Display`.
        rel = rel.split("|", 1)[0]
        candidate = (self.root / rel).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"path escapes vault root: {relative!r}") from exc
        return candidate

    def relative(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.root))
        except ValueError:
            return str(path)

    # -- Read ----------------------------------------------------------------

    def read_note(self, relative: str) -> dict[str, Any]:
        """Read a note. Accepts ``Note``, ``Note.md``, ``Folder/Note``, or ``[[Note]]``."""
        candidate = self._resolve_note(relative)
        if not candidate or not candidate.is_file():
            raise FileNotFoundError(f"note not found in vault: {relative!r}")
        text = candidate.read_text(encoding="utf-8", errors="replace")
        frontmatter, body = parse_frontmatter(text)
        return {
            "path": self.relative(candidate),
            "frontmatter": frontmatter,
            "body": body,
            "size": candidate.stat().st_size,
        }

    def _resolve_note(self, relative: str) -> Path | None:
        """Find a note by relative path, with or without ``.md``, anywhere in the vault."""
        try:
            direct = self._safe_path(relative)
        except ValueError:
            return None
        if direct.suffix.lower() == ".md" and direct.is_file():
            return direct
        with_ext = direct.with_suffix(".md")
        if with_ext.is_file():
            return with_ext
        # Fall back to a recursive name match (Obsidian wikilink resolution).
        name = direct.name
        if not name:
            return None
        target_lower = (name if name.endswith(".md") else f"{name}.md").lower()
        for found in self.root.rglob("*.md"):
            if found.name.lower() == target_lower:
                return found
        return None

    # -- Write ---------------------------------------------------------------

    def write_note(
        self,
        relative: str,
        body: str,
        *,
        frontmatter: dict[str, Any] | None = None,
        overwrite: bool = False,
    ) -> Path:
        """Create (or overwrite) a markdown note under the vault root."""
        target = self._safe_path(relative)
        if target.suffix.lower() != ".md":
            target = target.with_suffix(".md")
        if target.exists() and not overwrite:
            raise FileExistsError(f"note already exists: {self.relative(target)}")
        target.parent.mkdir(parents=True, exist_ok=True)
        content = self._compose_note(frontmatter or {}, body or "")
        with self._write_lock:
            target.write_text(content, encoding="utf-8")
        return target

    def append_note(
        self,
        relative: str,
        body: str,
        *,
        frontmatter_updates: dict[str, Any] | None = None,
        create_if_missing: bool = True,
        initial_frontmatter: dict[str, Any] | None = None,
    ) -> Path:
        """Append ``body`` to a note. Creates the file if missing when allowed."""
        target = self._safe_path(relative)
        if target.suffix.lower() != ".md":
            target = target.with_suffix(".md")
        with self._write_lock:
            if target.exists():
                existing = target.read_text(encoding="utf-8", errors="replace")
                fm, existing_body = parse_frontmatter(existing)
                if frontmatter_updates:
                    fm = _merge_frontmatter(fm, frontmatter_updates)
                new_body = existing_body.rstrip() + "\n\n" + body.lstrip() if existing_body.strip() else body
                content = self._compose_note(fm, new_body)
            else:
                if not create_if_missing:
                    raise FileNotFoundError(f"note not found: {self.relative(target)}")
                fm = dict(initial_frontmatter or {})
                if frontmatter_updates:
                    fm = _merge_frontmatter(fm, frontmatter_updates)
                content = self._compose_note(fm, body)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return target

    def _compose_note(self, frontmatter: dict[str, Any], body: str) -> str:
        fm_block = dump_frontmatter(frontmatter)
        if fm_block:
            return f"{fm_block}\n{body.rstrip()}\n"
        return f"{body.rstrip()}\n"

    # -- Search --------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        limit: int = 5,
        folder: str | None = None,
        snippet_window: int = 220,
    ) -> list[dict[str, Any]]:
        """Lightweight content+filename search.

        Scoring: each query term contributes (count_in_body * 1) +
        (count_in_filename * 5) + (count_in_frontmatter * 2). Results are
        sorted by score descending, then by modification time.
        """
        if not self.config.search_enabled or not query.strip():
            return []
        terms = [t.lower() for t in re.split(r"\s+", query.strip()) if t]
        if not terms:
            return []
        base = self.root
        if folder:
            try:
                base = self._safe_path(folder)
            except ValueError:
                return []
            if not base.is_dir():
                return []

        results: list[tuple[float, dict[str, Any]]] = []
        for md_path in base.rglob("*.md"):
            if not md_path.is_file():
                continue
            try:
                text = md_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fm, body = parse_frontmatter(text)
            fm_blob = json.dumps(fm, default=str).lower() if fm else ""
            body_lower = body.lower()
            name_lower = md_path.name.lower()
            score = 0.0
            for term in terms:
                score += body_lower.count(term)
                score += fm_blob.count(term) * 2
                score += name_lower.count(term) * 5
            if score <= 0:
                continue
            snippet = self._snippet(body, terms, snippet_window)
            results.append((
                score,
                {
                    "path": self.relative(md_path),
                    "score": score,
                    "mtime": md_path.stat().st_mtime,
                    "snippet": snippet,
                    "frontmatter_keys": sorted(fm.keys())[:10],
                },
            ))
        results.sort(key=lambda item: (item[0], item[1]["mtime"]), reverse=True)
        return [payload for _score, payload in results[:limit]]

    @staticmethod
    def _snippet(body: str, terms: Iterable[str], window: int) -> str:
        if not body:
            return ""
        lower = body.lower()
        positions = [lower.find(term) for term in terms if term]
        positions = [p for p in positions if p >= 0]
        if not positions:
            return body[:window].strip()
        center = min(positions)
        half = max(window // 2, 40)
        start = max(0, center - half)
        end = min(len(body), center + half)
        snippet = body[start:end].strip()
        if start > 0:
            snippet = "…" + snippet
        if end < len(body):
            snippet = snippet + "…"
        return snippet

    def list_notes(
        self,
        *,
        folder: str | None = None,
        limit: int = 50,
        tag: str | None = None,
    ) -> list[dict[str, Any]]:
        base = self.root
        if folder:
            try:
                base = self._safe_path(folder)
            except ValueError:
                return []
            if not base.is_dir():
                return []
        out: list[dict[str, Any]] = []
        for md in base.rglob("*.md"):
            if not md.is_file():
                continue
            entry = {
                "path": self.relative(md),
                "mtime": md.stat().st_mtime,
                "size": md.stat().st_size,
            }
            if tag:
                try:
                    fm, _ = parse_frontmatter(md.read_text(encoding="utf-8", errors="replace"))
                except OSError:
                    continue
                tags = fm.get("tags") or []
                if isinstance(tags, str):
                    tags = [tags]
                if tag not in [str(t) for t in tags]:
                    continue
            out.append(entry)
        out.sort(key=lambda e: e["mtime"], reverse=True)
        return out[:limit]

    # -- Link formatting -----------------------------------------------------

    def format_link(self, relative_path: str, *, label: str | None = None) -> str:
        rel = relative_path.replace("\\", "/")
        if rel.lower().endswith(".md"):
            rel_no_ext = rel[:-3]
        else:
            rel_no_ext = rel
        if self.config.link_style == "markdown":
            target = rel if rel.endswith(".md") else f"{rel}.md"
            return f"[{label or rel_no_ext}]({target})"
        # default: wikilink
        if label and label != rel_no_ext:
            return f"[[{rel_no_ext}|{label}]]"
        return f"[[{rel_no_ext}]]"

    # -- Session-note path helpers ------------------------------------------

    def session_note_path(self, session_id: str, *, started_at: _dt.datetime | None = None) -> str:
        when = started_at or _dt.datetime.now()
        day = when.strftime("%Y-%m-%d")
        base = self.config.sessions_folder.strip("/")
        slug = slugify(session_id or "session", max_length=64)
        return f"{base}/{day}/{slug}.md"

    def daily_note_path(self, *, when: _dt.datetime | None = None) -> str:
        when = when or _dt.datetime.now()
        base = self.config.sessions_folder.strip("/")
        return f"{base}/Daily/{when.strftime('%Y-%m-%d')}.md"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _merge_frontmatter(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in (updates or {}).items():
        if key in merged and isinstance(merged[key], list) and isinstance(value, list):
            seen = set()
            combined: list[Any] = []
            for item in [*merged[key], *value]:
                marker = json.dumps(item, default=str, sort_keys=True)
                if marker in seen:
                    continue
                seen.add(marker)
                combined.append(item)
            merged[key] = combined
            continue
        merged[key] = value
    return merged
