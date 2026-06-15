import shutil
from pathlib import Path
from typing import Any


def build_tree(cfg: Any) -> None:
    temp_dir = Path("temp_transformed")
    target_dir = Path(cfg.output.target_dir)

    src_dir = target_dir / "src"
    include_dir = target_dir / "include"

    if target_dir.exists():
        shutil.rmtree(target_dir)

    src_dir.mkdir(parents=True, exist_ok=True)
    include_dir.mkdir(parents=True, exist_ok=True)

    if not temp_dir.exists():
        print(f"Warning: {temp_dir} does not exist, nothing to assemble")
        return

    for module_dir in temp_dir.iterdir():
        if not module_dir.is_dir():
            continue
        module_name = module_dir.name

        mod_src_dir = src_dir / module_name
        mod_inc_dir = include_dir / module_name
        mod_src_dir.mkdir(parents=True, exist_ok=True)
        mod_inc_dir.mkdir(parents=True, exist_ok=True)

        for cpp_file in module_dir.glob("*.cpp"):
            shutil.copy2(cpp_file, mod_src_dir / cpp_file.name)
        for h_file in module_dir.glob("*.h"):
            shutil.copy2(h_file, mod_inc_dir / h_file.name)

    report_src = Path("cr-agent-report.json")
    if report_src.exists():
        shutil.copy2(report_src, target_dir / report_src.name)

    from re_agent.build.assemble.cmake_generator import generate_cmake
    from re_agent.build.assemble.conflict_resolver import resolve_conflicts
    from re_agent.build.assemble.header_generator import generate_common_header

    generate_common_header(cfg)
    resolve_conflicts(cfg)
    generate_cmake(cfg)
