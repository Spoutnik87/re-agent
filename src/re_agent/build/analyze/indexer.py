"""Index modules into sub-units using TF-IDF similarity grouping."""

import json
from pathlib import Path
from typing import Any

from sklearn.feature_extraction.text import TfidfVectorizer  # type: ignore[import-not-found]
from sklearn.metrics.pairwise import cosine_similarity  # type: ignore[import-not-found]


def _read_function_source(decompiled_dir: Path, addr: str) -> str:
    candidates = list(decompiled_dir.glob(f"{addr}*.cpp"))
    if candidates:
        return candidates[0].read_text(encoding="utf-8", errors="replace")
    return ""


def _group_by_similarity(
    addrs: list[str],
    sources: list[str],
    subunit_size: int,
) -> list[list[str]]:
    remaining = set(range(len(addrs)))
    groups: list[list[str]] = []

    max_features = 500
    while max_features >= 50:
        try:
            vectorizer = TfidfVectorizer(max_features=max_features)
            tfidf_matrix = vectorizer.fit_transform(sources)
            break
        except ValueError:
            max_features //= 2
    else:
        groups.append(list(addrs))
        return groups

    sim_matrix = cosine_similarity(tfidf_matrix)

    while remaining:
        seed = min(remaining)
        remaining.remove(seed)
        group = [addrs[seed]]
        group_indices = [seed]

        while len(group) < subunit_size and remaining:
            last_idx = group_indices[-1]
            scores = [(i, sim_matrix[last_idx, i]) for i in remaining]
            scores.sort(key=lambda x: x[1], reverse=True)

            n_take = min(subunit_size - len(group), len(scores))
            for idx, _ in scores[:n_take]:
                remaining.remove(idx)
                group.append(addrs[idx])
                group_indices.append(idx)

        groups.append(group)

    return groups


def index_modules(modules_data: dict[str, Any], cfg: Any) -> None:
    decompiled_dir = Path(cfg.input.decompiled_dir)
    subunit_size = cfg.optimization.subunit_size

    for mod_info in modules_data["modules"].values():
        functions = mod_info["functions"]
        if mod_info["size"] <= subunit_size:
            mod_info["sub_units"] = [list(functions)]
        else:
            source_map: dict[str, str] = {}
            for addr in functions:
                candidates = list(decompiled_dir.glob(f"{addr}*.cpp"))
                if candidates:
                    source_map[addr] = candidates[0].read_text(encoding="utf-8", errors="replace")
            sources = [source_map.get(addr, "") for addr in functions]
            mod_info["sub_units"] = _group_by_similarity(
                functions,
                sources,
                subunit_size,
            )

    with open(Path(cfg.output.work_dir) / "modules.json", "w", encoding="utf-8") as f:
        json.dump(modules_data, f, indent=2)

    print("Sub-units saved to modules.json")
