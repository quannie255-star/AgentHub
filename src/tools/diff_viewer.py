"""Code Diff tool — generate unified diffs for agent-produced code changes.

Agents call this when they want to show a before/after comparison of a file.
Supports both in-memory strings and on-disk files.
"""

from __future__ import annotations

import difflib
from pathlib import Path

from src.core.schema import DiffResult

# Maximum file size for diff operations (1 MB default)
DEFAULT_MAX_FILE_SIZE = 1_048_576


def compute_diff(
    original: str,
    modified: str,
    file_path: str = "",
    language: str = "",
    context_lines: int = 3,
) -> DiffResult:
    """Compute a unified diff between two strings.

    Args:
        original: The original file content.
        modified: The modified file content.
        file_path: Logical file path (used in diff header).
        language: Programming language for syntax-highlighting hint.
        context_lines: Number of context lines in the diff.

    Returns:
        ``DiffResult`` with the unified diff string.

    Example:
        >>> result = compute_diff("print('old')", "print('new')", file_path="main.py")
        >>> assert "@@" in result.unified_diff
    """
    from_file = file_path or "original"
    to_file = file_path or "modified"

    original_lines = original.splitlines(keepends=True)
    modified_lines = modified.splitlines(keepends=True)

    diff_lines = list(
        difflib.unified_diff(
            original_lines,
            modified_lines,
            fromfile=f"a/{from_file}",
            tofile=f"b/{to_file}",
            n=context_lines,
        )
    )

    # If lines don't end with newline, add one
    normalized = []
    for line in diff_lines:
        if not line.endswith("\n"):
            normalized.append(line + "\n")
        else:
            normalized.append(line)

    unified = "".join(normalized)

    # Auto-detect language from file extension
    if not language and file_path:
        language = _detect_language(file_path)

    return DiffResult(
        file_path=file_path,
        original=original,
        modified=modified,
        unified_diff=unified,
        language=language,
    )


def diff_files(
    original_path: str | Path,
    modified_path: str | Path,
    max_size: int = DEFAULT_MAX_FILE_SIZE,
) -> DiffResult:
    """Compute a unified diff between two files on disk.

    Args:
        original_path: Path to the original file.
        modified_path: Path to the modified file.
        max_size: Maximum file size in bytes (files larger are rejected).

    Returns:
        ``DiffResult`` with the diff.

    Raises:
        FileNotFoundError: If either file doesn't exist.
        ValueError: If either file exceeds ``max_size``.
    """
    op = Path(original_path)
    mp = Path(modified_path)

    if not op.exists():
        raise FileNotFoundError(f"Original file not found: {op}")
    if not mp.exists():
        raise FileNotFoundError(f"Modified file not found: {mp}")

    if op.stat().st_size > max_size:
        raise ValueError(
            f"Original file exceeds max size ({max_size} bytes): {op}"
        )
    if mp.stat().st_size > max_size:
        raise ValueError(
            f"Modified file exceeds max size ({max_size} bytes): {mp}"
        )

    original = op.read_text(encoding="utf-8")
    modified = mp.read_text(encoding="utf-8")
    language = _detect_language(str(op))

    return compute_diff(
        original=original,
        modified=modified,
        file_path=str(op),
        language=language,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LANGUAGE_MAP: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".jsx": "jsx",
    ".css": "css",
    ".html": "html",
    ".json": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".md": "markdown",
    ".sql": "sql",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".rb": "ruby",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".toml": "toml",
    ".sh": "bash",
    ".bat": "bat",
    ".dockerfile": "dockerfile",
    ".txt": "text",
}


def _detect_language(file_path: str) -> str:
    """Guess programming language from file extension."""
    suffix = Path(file_path).suffix.lower()
    # Special case: Dockerfile
    if Path(file_path).name.lower() == "dockerfile":
        return "dockerfile"
    return _LANGUAGE_MAP.get(suffix, "")
