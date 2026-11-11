"""SEARCH/REPLACE edit engine for the agent shell.

The parsers and matchers are imported from aider's editblock coder so the
fork's edit-quality fixes (indentation handling, whitespace matching) apply
unchanged.  Unlike Coder.apply_updates(), this module has no IO/git/permission
coupling: it turns edit-format text into per-file results.
"""

from __future__ import annotations

import difflib
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from aider.coders.editblock_coder import (
    DEFAULT_FENCE,
    find_original_update_blocks,
    find_similar_lines,
    replace_most_similar_chunk,
    strip_quoted_wrapping,
)


class EditPathError(ValueError):
    """Raised when an edit block points outside the repository."""


@dataclass
class FileEditResult:
    path: str
    status: str  # applied | created | failed
    blocks: int = 0
    added: int = 0
    removed: int = 0
    error: str | None = None
    did_you_mean: str | None = None
    block_index: int | None = None
    diff: str = ""


@dataclass
class EditResult:
    ok: bool
    files: list[FileEditResult] = field(default_factory=list)
    errors: list[FileEditResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "files": [asdict(f) for f in self.files],
            "errors": [asdict(e) for e in self.errors],
            "warnings": self.warnings,
        }


def resolve_path(root, rel: str) -> Path:
    """Resolve a repo-relative path, refusing anything outside the root."""
    rel = (rel or "").strip().replace("\\", "/")
    if not rel:
        raise EditPathError("empty file path")
    if rel.startswith("~"):
        raise EditPathError(f"refusing home-relative path: {rel}")
    candidate = Path(rel)
    if candidate.is_absolute():
        raise EditPathError(f"refusing absolute path: {rel}")
    root = Path(root).resolve()
    try:
        full = (root / candidate).resolve()
    except OSError as exc:
        raise EditPathError(f"cannot resolve path: {rel} ({exc})") from exc
    try:
        full.relative_to(root)
    except ValueError:
        raise EditPathError(f"path escapes the repository: {rel}") from None
    return full


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _read_text(full: Path) -> tuple[str, str]:
    raw = full.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    text = raw.decode("utf-8", errors="replace")
    return _normalize_newlines(text), newline


def _write_text(full: Path, text: str, newline: str) -> None:
    text = _normalize_newlines(text)
    if newline == "\r\n":
        text = text.replace("\n", "\r\n")
    full.write_bytes(text.encode("utf-8"))


def _diff_stats(before: str, after: str, path: str = "") -> tuple[int, int, str]:
    lines = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=path or "before",
            tofile=path or "after",
            lineterm="",
            n=2,
        )
    )
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    return added, removed, "\n".join(lines)


def _display_path(path: str) -> str:
    return os.path.normpath(path.replace("\\", "/")).replace("\\", "/")


def _did_you_mean(original: str, content: str) -> str | None:
    """Best-effort nearest text for a failed SEARCH block.

    find_similar_lines() only scores line-for-line equality, so a one-line
    near miss scores 0.  Fall back to a character-level comparison over
    same-sized windows.
    """
    similar = find_similar_lines(original, content)
    if similar:
        return similar

    search_lines = original.splitlines()
    content_lines = content.splitlines()
    if not search_lines or not content_lines:
        return None

    width = len(search_lines)
    search_text = "\n".join(search_lines)
    best_ratio = 0.0
    best_window = None
    for start in range(max(1, len(content_lines) - width + 1)):
        window = content_lines[start : start + width]
        ratio = difflib.SequenceMatcher(None, search_text, "\n".join(window)).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_window = window

    if best_window and best_ratio >= 0.5:
        return "\n".join(best_window)
    return None


def _apply_file(root: Path, rel: str, blocks: list[tuple[str, str]]) -> FileEditResult:
    try:
        full = resolve_path(root, rel)
    except EditPathError as exc:
        return FileEditResult(path=rel, status="failed", error=str(exc))

    created = not full.exists()
    if created:
        before, newline = "", "\n"
    else:
        try:
            before, newline = _read_text(full)
        except OSError as exc:
            return FileEditResult(path=rel, status="failed", error=f"cannot read file: {exc}")

    content = before
    exists = not created

    for block_index, (original, updated) in enumerate(blocks, start=1):
        original = strip_quoted_wrapping(_normalize_newlines(original), rel, DEFAULT_FENCE) or ""
        updated = strip_quoted_wrapping(_normalize_newlines(updated), rel, DEFAULT_FENCE) or ""

        if not exists:
            if original.strip():
                return FileEditResult(
                    path=rel,
                    status="failed",
                    block_index=block_index,
                    error="file does not exist; use write_file to create it",
                )
            content = updated
            exists = True
            continue

        if not original.strip():
            new_content = content + updated
        else:
            new_content = replace_most_similar_chunk(content, original, updated)

        if new_content is None:
            similar = _did_you_mean(original, content)
            return FileEditResult(
                path=rel,
                status="failed",
                block_index=block_index,
                error="SEARCH text not found (or not unique) in the file",
                did_you_mean=similar or None,
            )
        content = new_content

    try:
        if created:
            full.parent.mkdir(parents=True, exist_ok=True)
        if content != before:
            _write_text(full, content, newline)
    except OSError as exc:
        return FileEditResult(path=rel, status="failed", error=f"cannot write file: {exc}")

    added, removed, diff = _diff_stats(before, content, rel)
    return FileEditResult(
        path=rel,
        status="created" if created else "applied",
        blocks=len(blocks),
        added=added,
        removed=removed,
        diff=diff,
    )


def apply_edit_text(text: str, root, valid_fnames=None) -> EditResult:
    """Parse SEARCH/REPLACE blocks and apply them, one file at a time.

    Each file is all-or-nothing: if one block for that file fails, the file is
    left untouched and the failure is reported so the model can retry.
    """
    root = Path(root).resolve()
    result = EditResult(ok=True)
    if not text or not text.strip():
        result.ok = False
        result.errors.append(FileEditResult(path="", status="failed", error="empty edit text"))
        return result

    text = _normalize_newlines(text)

    try:
        raw_blocks = list(
            find_original_update_blocks(text, DEFAULT_FENCE, list(valid_fnames or []))
        )
    except ValueError as exc:
        result.ok = False
        result.errors.append(FileEditResult(path="", status="failed", error=str(exc).strip()))
        return result

    groups: dict[str, dict] = {}
    order: list[str] = []
    shell_blocks = 0

    for block in raw_blocks:
        if block[0] is None:
            shell_blocks += 1
            continue
        path, original, updated = block
        display = _display_path(path)
        key = os.path.normcase(display)
        if key not in groups:
            groups[key] = {"path": display, "blocks": []}
            order.append(key)
        groups[key]["blocks"].append((original, updated))

    if shell_blocks:
        result.warnings.append(
            f"{shell_blocks} shell block(s) ignored; use the run_command tool instead"
        )

    for key in order:
        group = groups[key]
        file_result = _apply_file(root, group["path"], group["blocks"])
        result.files.append(file_result)
        if file_result.status == "failed":
            result.ok = False
            result.errors.append(file_result)

    return result
