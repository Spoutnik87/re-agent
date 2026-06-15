from pathlib import Path
from typing import Any


def resolve_conflicts(cfg: Any) -> None:
    target_dir = Path(cfg.output.target_dir)
    src_dir = target_dir / "src"

    symbols: dict[str, set[str]] = {}
    for module_dir in src_dir.iterdir():
        if not module_dir.is_dir():
            continue
        module_name = module_dir.name
        symbols[module_name] = set()
        for cpp_file in module_dir.glob("*.cpp"):
            content = cpp_file.read_text(encoding="utf-8", errors="replace")
            for line in content.split("\n"):
                stripped = line.strip()
                if "(" in stripped and any(
                    stripped.startswith(kw) for kw in ["void ", "int ", "bool ", "float ", "double ", "char ", "long "]
                ):
                    name = stripped.split("(")[0].split()[-1].rstrip("(")
                    symbols[module_name].add(name)

    all_symbols: dict[str, list[str]] = {}
    for module, syms in symbols.items():
        for s in syms:
            all_symbols.setdefault(s, []).append(module)

    conflicts = {s: mods for s, mods in all_symbols.items() if len(mods) > 1}
    if conflicts:
        print(f"Cross-module conflicts found: {len(conflicts)}")
        for sym, mods in list(conflicts.items())[:5]:
            print(f"  {sym}: {mods}")
    else:
        print("No cross-module conflicts detected")
