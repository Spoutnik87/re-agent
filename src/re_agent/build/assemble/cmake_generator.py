from pathlib import Path
from typing import Any


def generate_cmake(cfg: Any) -> None:
    target_dir = Path(cfg.output.target_dir)
    src_dir = target_dir / "src"

    modules = [d.name for d in src_dir.iterdir() if d.is_dir()]

    cmake_lines = [
        "cmake_minimum_required(VERSION 3.16)",
        f"project({cfg.project.name or 'reconstructed_project'})",
        "",
        "set(CMAKE_CXX_STANDARD 23)",
        "set(CMAKE_CXX_STANDARD_REQUIRED ON)",
        'set(CMAKE_CXX_FLAGS "-m32 -Wall")',
        "",
    ]

    for module in modules:
        cmake_lines.extend(
            [
                f"add_library({module} STATIC",
                f"    src/{module}/*.cpp",
                ")",
                f"target_include_directories({module} PUBLIC include/{module} include)",
                "",
            ]
        )

    cmake_path = target_dir / "CMakeLists.txt"
    cmake_path.write_text("\n".join(cmake_lines), encoding="utf-8")
    print(f"Generated {cmake_path} with {len(modules)} module libraries")
