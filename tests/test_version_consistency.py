"""Verify version consistency across all version declarations.

Checks that ``pyproject.toml``, ``src/re_agent/__init__.py``,
``src/re_agent/build/__init__.py``, and ``src/re_agent/cli/main.py``
all declare the same version string.
"""

from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent


def _version_from_init(package_dir: str) -> str:
    """Parse __version__ from a package __init__.py."""
    init_py = REPO / "src" / package_dir / "__init__.py"
    for line in init_py.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=")[1].strip().strip("\"'")
    msg = f"__version__ not found in {init_py}"
    raise AssertionError(msg)


def _version_from_pyproject() -> str:
    """Parse version from pyproject.toml."""
    toml = REPO / "pyproject.toml"
    for line in toml.read_text(encoding="utf-8").splitlines():
        if line.startswith("version"):
            return line.split("=")[1].strip().strip("\"'")
    msg = "version not found in pyproject.toml"
    raise AssertionError(msg)


def _version_from_cli() -> str:
    """Parse version from the CLI --version string."""
    cli_py = REPO / "src" / "re_agent" / "cli" / "main.py"
    for line in cli_py.read_text(encoding="utf-8").splitlines():
        if "--version" in line and "version" in line:
            # Extract: version="%(prog)s X.Y.Z"
            import re

            m = re.search(r'version="[^"]*\s+(\d+\.\d+\.\d+)"', line)
            if m:
                return m.group(1)
    msg = "version not found in CLI main.py"
    raise AssertionError(msg)


def test_all_versions_consistent() -> None:
    pyproject = _version_from_pyproject()
    re_agent = _version_from_init("re_agent")
    build = _version_from_init("re_agent/build")
    cli = _version_from_cli()

    assert pyproject == re_agent == build == cli, (
        f"Version mismatch: pyproject={pyproject}, re_agent={re_agent}, build={build}, cli={cli}"
    )


def test_version_format() -> None:
    """Version must be a valid semver X.Y.Z."""
    v = _version_from_pyproject()
    parts = v.split(".")
    assert len(parts) == 3, f"Expected X.Y.Z format, got {v}"
    assert all(p.isdigit() for p in parts), f"All parts must be numeric, got {v}"
