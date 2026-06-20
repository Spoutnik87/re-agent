import re
from pathlib import Path
from typing import Any

_FUNC_DECL_RE = re.compile(r"(?:^|\n)([\w\s\*&<>:]+)\s+(\w+)\s*\([^)]*\)\s*;", re.MULTILINE)


def generate_common_header(cfg: Any) -> None:
    target_dir = Path(cfg.output.target_dir)
    include_dir = target_dir / "include"

    forward_decls = []
    for hdr in include_dir.rglob("*.h"):
        content = hdr.read_text(encoding="utf-8", errors="replace")
        for match in _FUNC_DECL_RE.finditer(content):
            forward_decls.append(f"{match.group(1).strip()} {match.group(2)};")

    common_h = include_dir / "common.h"
    unique_decls = sorted(set(forward_decls))
    common_h.write_text(
        "#pragma once\n\n" + "\n".join(unique_decls) + "\n",
        encoding="utf-8",
    )
