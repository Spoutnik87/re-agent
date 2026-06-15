"""Build an undirected call-graph from decompiled .cpp files."""

import re
from pathlib import Path
from typing import Any

FILE_ADDR_RE = re.compile(r"(0x[0-9A-Fa-f]{6,8})")
CALL_FUN_RE = re.compile(r"FUN_([0-9A-Fa-f]{6,8})")
CALL_HEX_RE = re.compile(r"\b(0x[0-9A-Fa-f]{6,8})\b")


def build_graph(cfg: Any) -> dict[str, set[str]]:
    decompiled_dir = Path(cfg.input.decompiled_dir)
    cpp_files = sorted(decompiled_dir.glob("*.cpp"))

    file_addresses: dict[str, str] = {}
    for f in cpp_files:
        m = FILE_ADDR_RE.search(f.name)
        if m:
            file_addresses[f.name] = m.group(1)

    address_set: set[str] = set(file_addresses.values())

    graph: dict[str, set[str]] = {}

    print(f"Parsing {len(cpp_files)} files...")

    for f in cpp_files:
        addr = file_addresses.get(f.name)
        if addr is None:
            continue
        graph.setdefault(addr, set())

        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        for m in CALL_FUN_RE.finditer(content):
            called = "0x" + m.group(1)
            if called in address_set and called != addr:
                graph[addr].add(called)

        for m in CALL_HEX_RE.finditer(content):
            called = m.group(1)
            if called in address_set and called != addr:
                graph[addr].add(called)

    print(f"Graph built: {len(graph)} nodes")
    return graph
