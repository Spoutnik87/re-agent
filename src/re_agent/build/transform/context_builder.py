from __future__ import annotations

from pathlib import Path
from typing import Any


def _read_function_source(
    decompiled_dir: Path,
    addr: str,
    source_map: dict[str, str] | None = None,
) -> str:
    if source_map is not None and addr in source_map:
        return source_map[addr]
    candidates = list(decompiled_dir.glob(f"{addr}*.cpp"))
    if candidates:
        return candidates[0].read_text(encoding="utf-8", errors="replace")
    return ""


def get_neighbours(
    function_addresses: list[str],
    current_address: str,
    decompiled_dir: Path,
    context_window: int,
    source_map: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    neighbours: list[dict[str, str]] = []
    try:
        idx = function_addresses.index(current_address)
    except ValueError:
        return neighbours

    start = max(0, idx - context_window)
    end = min(len(function_addresses), idx + context_window + 1)

    for i in range(start, end):
        addr = function_addresses[i]
        if addr == current_address:
            continue
        code = _read_function_source(decompiled_dir, addr, source_map)
        neighbours.append({"address": addr, "code": code})

    return neighbours


def build_context(
    subunit: list[str],
    module_functions: list[str],
    decompiled_dir: Path,
    context_window: int,
    cache: Any,
    prompt_hash: str = "",
    model: str = "",
    source_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    if not subunit:
        return {"functions_to_transform": [], "neighbour_context": [], "cached_count": 0}

    neighbour_context = get_neighbours(
        module_functions,
        subunit[0],
        decompiled_dir,
        context_window,
        source_map,
    )

    functions_to_transform: list[dict[str, str]] = []
    cached_count = 0

    for addr in subunit:
        code = _read_function_source(decompiled_dir, addr, source_map)
        if cache is not None and cache.has(addr, code, prompt_hash=prompt_hash, model=model):
            cached_count += 1
            continue
        functions_to_transform.append({"address": addr, "code": code})

    return {
        "functions_to_transform": functions_to_transform,
        "neighbour_context": neighbour_context,
        "cached_count": cached_count,
    }
