"""Few-shot example builder — retrieves similar already-decompiled functions.

Indexes successful decompilations from the code directory by structural
features (line count, vtable call density, global reference count) and
returns 1–2 similar examples to inject into the reverser prompt.

Lazy-loading: the index is built on first access and cached in memory.
"""

from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Regex for feature extraction
# ---------------------------------------------------------------------------

VTABLE_RE = re.compile(
    r"reinterpret_cast.*vtable|\(\*\(\*\(code\s*\*\*\)\(",
    re.I,
)
GLOBAL_RE = re.compile(
    r"\bg_\w+|DAT_\w{8}|extern\s+\w+\s+\w+",
)
CALL_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_:]*)\s*\(",
)


def _bucket(n: int, boundaries: tuple[int, ...] = (25, 50, 100, 200)) -> str:
    for b in boundaries:
        if n < b:
            return f"<{b}l"
    return f"{boundaries[-1]}+l"


def _vtable_bucket(n: int) -> str:
    if n == 0:
        return "no-vtable"
    if n <= 3:
        return "light-vtable"
    return "heavy-vtable"


def _extract_features(code_text: str) -> dict:
    vtable = len(VTABLE_RE.findall(code_text))
    globals_count = len(GLOBAL_RE.findall(code_text))

    call_count = 0
    for m in CALL_RE.finditer(code_text):
        tok = m.group(1)
        if tok in {
            "if",
            "for",
            "while",
            "switch",
            "return",
            "sizeof",
            "decltype",
            "static_cast",
            "reinterpret_cast",
            "const_cast",
            "dynamic_cast",
            "catch",
            "new",
            "delete",
        }:
            continue
        call_count += 1

    return {
        "lines": code_text.count("\n") + 1,
        "vtable": vtable,
        "globals": globals_count,
        "calls": call_count,
        "line_bucket": _bucket(code_text.count("\n") + 1),
        "vtable_bucket": _vtable_bucket(vtable),
    }


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


class FewShotBuilder:
    """Builds and queries an index of successfully decompiled functions."""

    _instance: FewShotBuilder | None = None
    _index: list[dict] | None = None

    def __init__(self, code_dir: Path, max_examples: int = 2, max_lines: int = 30):
        self._code_dir = code_dir
        self._max_examples = max_examples
        self._max_lines = max_lines

    @classmethod
    def singleton(cls, code_dir: Path) -> FewShotBuilder:
        """Get or create a singleton instance."""
        if cls._instance is None:
            cls._instance = cls(code_dir)
        return cls._instance

    @classmethod
    def clear_cache(cls) -> None:
        """Clear the singleton and cached index (e.g., between batch runs)."""
        cls._instance = None
        cls._index = None

    # ------------------------------------------------------------------
    # Indexing
    # ------------------------------------------------------------------

    def _ensure_index(self) -> None:
        if FewShotBuilder._index is not None:
            return

        if not self._code_dir.exists():
            FewShotBuilder._index = []
            return

        entries: list[dict] = []
        for path in self._code_dir.iterdir():
            if path.suffix != ".cpp":
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if len(text) < 30:
                continue
            if "placeholder" in text.lower() or "This does NOT match" in text:
                continue
            entries.append({"path": path, "text": text})

        # Extract features and index
        indexed: list[dict] = []
        for e in entries:
            features = _extract_features(e["text"])
            features["path"] = e["path"]
            features["text"] = e["text"]
            indexed.append(features)

        FewShotBuilder._index = indexed

    # ------------------------------------------------------------------
    # Querying
    # ------------------------------------------------------------------

    def find_similar(self, decompiled: str, max_examples: int = 0) -> list[str]:
        """Find 1–2 similar reverse-engineered examples.

        Args:
            decompiled: Ghidra decompiled text to characterize the target.
            max_examples: Override the instance default (0 = use default).

        Returns:
            List of trimmed example snippets ready for prompt injection.
        """
        self._ensure_index()
        index = FewShotBuilder._index
        if not index:
            return []

        max_ex = max_examples if max_examples else self._max_examples

        target = _extract_features(decompiled)

        # Score each candidate against the target
        scored: list[tuple[int, dict]] = []
        for entry in index:
            score = 0
            if entry["line_bucket"] == target["line_bucket"]:
                score += 3
            if entry["vtable_bucket"] == target["vtable_bucket"]:
                score += 3
            if abs(entry["globals"] - target["globals"]) <= 2:
                score += 1
            if abs(entry["calls"] - target["calls"]) <= 3:
                score += 1
            scored.append((score, entry))

        scored.sort(key=lambda x: -x[0])

        examples: list[str] = []
        seen_paths: set[Path] = set()
        for _score, entry in scored:
            if len(examples) >= max_ex:
                break
            if entry["path"] in seen_paths:
                continue
            seen_paths.add(entry["path"])
            trimmed = self._trim(entry["text"])
            name = entry["path"].stem.replace("0x", "")
            examples.append(f"// Example from {name}:\n```cpp\n{trimmed}\n```")

        return examples

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _trim(self, text: str) -> str:
        lines = text.strip().splitlines()
        if len(lines) <= self._max_lines:
            return "\n".join(lines)
        # Keep first 5 lines (includes + declarations) and last lines
        head = lines[:5]
        tail = lines[-(self._max_lines - 6) :]
        return "\n".join(head) + "\n// ... (truncated)\n" + "\n".join(tail)
