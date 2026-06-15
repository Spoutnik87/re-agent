"""Cluster functions into modules using Louvain community detection."""

import json
from typing import Any

import networkx as nx  # type: ignore[import-untyped]
from networkx.algorithms.community import louvain_communities  # type: ignore[import-untyped]


def _build_graph(graph_dict: dict[str, set[str]]) -> nx.Graph:
    G = nx.Graph()
    for node, neighbors in graph_dict.items():
        G.add_node(node)
        for nbr in neighbors:
            G.add_edge(node, nbr)
    return G


def _name_clusters(
    communities: list[set[str]],
    expected_names: list[str],
) -> dict[str, dict[str, Any]]:
    modules: dict[str, dict[str, Any]] = {}
    counter = len(expected_names) + 1
    for i, members in enumerate(communities):
        if i < len(expected_names):
            name = expected_names[i]
        else:
            name = f"module_{counter}"
            counter += 1
        modules[name] = {
            "functions": sorted(members),
            "size": len(members),
        }
    return modules


def cluster(graph_dict: dict[str, set[str]], cfg: Any) -> dict[str, Any]:
    min_size = getattr(cfg.modules, "min_cluster_size", 20)
    max_size = getattr(cfg.modules, "max_cluster_size", 300)
    expected_names = getattr(cfg.modules, "expected", [])

    G = _build_graph(graph_dict)
    all_functions = set(graph_dict.keys())

    communities = list(louvain_communities(G, seed=42))

    orphans: set[str] = set()
    kept_communities: list[set[str]] = []

    for comm in communities:
        if len(comm) < min_size:
            orphans.update(comm)
        elif len(comm) > max_size:
            subgraph = G.subgraph(comm)
            sub_communities = list(louvain_communities(subgraph, seed=42))
            for sub_comm in sub_communities:
                if len(sub_comm) < min_size:
                    orphans.update(sub_comm)
                else:
                    kept_communities.append(sub_comm)
        else:
            kept_communities.append(comm)

    modules = _name_clusters(kept_communities, expected_names)

    assigned: set[str] = set()
    for mod in modules.values():
        assigned.update(mod["functions"])
    orphans.update(all_functions - assigned)

    metadata = {
        "total_functions": len(all_functions),
        "module_count": len(modules),
        "orphan_count": len(orphans),
    }

    result = {
        "modules": modules,
        "orphans": sorted(orphans),
        "metadata": metadata,
    }

    with open("modules.json", "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print("Modules saved to modules.json")
    return result
