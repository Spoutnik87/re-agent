"""Smoke tests for CLI."""

from __future__ import annotations

import hashlib
from pathlib import Path

from re_agent.cli.main import build_parser, main
from re_agent.contracts import Architecture, CallingConvention, Symbol, manifest_from_symbols, save_manifest


def _write_valid_cfg(tmp_path: Path, extra_yaml: str = "") -> Path:
    """Write a re-agent.yaml with valid contracts section + a real AbiManifest.

    Returns the path to the written YAML file (tmp_path / "re-agent.yaml").
    """
    manifest = manifest_from_symbols(
        version="1.0.0",
        architecture=Architecture.X86,
        pointer_size=4,
        symbols=[
            Symbol(
                address=0x1000,
                name="func_a",
                signature="void func_a()",
                calling_convention=CallingConvention.CDECL,
                output_path="mod_a.cpp",
            ),
        ],
    )
    manifest_path = tmp_path / "abi_manifest.json"
    save_manifest(manifest, manifest_path)
    raw_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    config_path = tmp_path / "re-agent.yaml"
    config_path.write_text(
        f"""{extra_yaml}
contracts:
  transformation_policy: "preserve_abi"
  abi_manifest_path: "{manifest_path.as_posix()}"
  abi_manifest_sha256: "{raw_hash}"
""",
        encoding="utf-8",
    )
    return config_path


def test_parser_builds() -> None:
    parser = build_parser()
    assert parser is not None


def test_no_command_returns_zero() -> None:
    assert main([]) == 0


def test_version_flag() -> None:
    import pytest

    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0


def test_init_creates_config(tmp_path: Path) -> None:
    """init --abi-manifest <valid> creates a reloadable config."""
    config_path = tmp_path / "re-agent.yaml"
    manifest = manifest_from_symbols(
        version="1.0.0",
        architecture=Architecture.X86,
        pointer_size=4,
        symbols=[
            Symbol(
                address=0x1000,
                name="func_a",
                signature="void func_a()",
                calling_convention=CallingConvention.CDECL,
                output_path="mod_a.cpp",
            ),
        ],
    )
    manifest_path = tmp_path / "abi_manifest.json"
    save_manifest(manifest, manifest_path)
    raw_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    result = main(["--config", str(config_path), "init", "--abi-manifest", str(manifest_path)])
    assert result == 0
    assert config_path.exists()
    # The generated config must be immediately reloadable
    from re_agent.config.loader import load_config

    cfg = load_config(config_path)
    assert cfg.contracts.transformation_policy == "preserve_abi"
    assert cfg.contracts.abi_manifest_sha256 == raw_hash


def test_init_fails_if_exists(tmp_path: Path) -> None:
    """init with existing config file returns 1."""
    config_path = tmp_path / "re-agent.yaml"
    config_path.write_text("existing")

    manifest = manifest_from_symbols(
        version="1.0.0",
        architecture=Architecture.X86,
        pointer_size=4,
        symbols=[
            Symbol(
                address=0x1000,
                name="func_a",
                signature="void func_a()",
                calling_convention=CallingConvention.CDECL,
                output_path="mod_a.cpp",
            ),
        ],
    )
    manifest_path = tmp_path / "abi_manifest.json"
    save_manifest(manifest, manifest_path)

    result = main(["--config", str(config_path), "init", "--abi-manifest", str(manifest_path)])
    assert result == 1


def test_init_without_abi_manifest_exits_2() -> None:
    """init without --abi-manifest exits 2."""
    import pytest

    with pytest.raises(SystemExit) as exc_info:
        main(["--config", "ignored.yaml", "init"])
    assert exc_info.value.code == 2


def test_init_with_invalid_manifest_exits_2(tmp_path: Path) -> None:
    """init with a non-existent manifest exits 2."""
    result = main(
        ["--config", str(tmp_path / "re-agent.yaml"), "init", "--abi-manifest", str(tmp_path / "nonexistent.json")]
    )
    assert result == 2


def test_status_no_session(tmp_path: Path) -> None:
    tp = tmp_path.as_posix()
    config_path = _write_valid_cfg(
        tmp_path,
        extra_yaml=f"""output:
  session_file: "{tp}/progress.json"
  report_dir: "{tp}/reports"
  log_dir: "{tp}/logs"
""",
    )
    result = main(["--config", str(config_path), "status"])
    assert result == 0


def test_reverse_dry_run(tmp_path: Path) -> None:
    config_path = _write_valid_cfg(tmp_path, extra_yaml="llm:\n  provider: claude\n")
    result = main(["--config", str(config_path), "reverse", "--address", "0x6F86A0", "--dry-run"])
    assert result == 0


def test_reverse_no_target(tmp_path: Path) -> None:
    config_path = _write_valid_cfg(tmp_path, extra_yaml="llm:\n  provider: claude\n")
    result = main(["--config", str(config_path), "reverse"])
    assert result == 1
