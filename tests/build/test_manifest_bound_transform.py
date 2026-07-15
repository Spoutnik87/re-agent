from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from re_agent.build.transform.manifest_bound_transform import (
    ManifestBoundTransformError,
    ManifestBoundVerdict,
    _compile_real,
    build_preserve_abi_prompt,
    parse_preserve_abi_response,
    run_manifest_bound_transform,
)
from re_agent.contracts.manifest import manifest_from_symbols
from re_agent.contracts.model import Architecture, CallingConvention, Symbol
from re_agent.contracts.runtime import VerifiedContract


@pytest.fixture
def symbol() -> Symbol:
    return Symbol(0x401000, "fn", "int fn(int)", CallingConvention.CDECL, "unit/fn.cpp")


@pytest.fixture
def manifest(symbol: Symbol):
    return manifest_from_symbols(version="1.0.0", architecture=Architecture.X86, pointer_size=4, symbols=[symbol])


def response(symbol: Symbol, body: str = "int fn(int x) { return x; }") -> str:
    return f"// TARGET: 0x{symbol.address:x}\n// FILE: {symbol.output_path}\n{body}"


def test_prompt_contains_only_contract_identity_and_source(symbol: Symbol, manifest) -> None:
    prompt = build_preserve_abi_prompt(symbol, "int fn(int x);", manifest)
    assert "0x401000" in prompt.user
    assert "fn" in prompt.user
    assert "int fn(int)" in prompt.user
    assert "cdecl" in prompt.user
    assert "unit/fn.cpp" in prompt.user
    assert "SOURCE\nint fn(int x);" in prompt.user
    assert "module" not in prompt.user


def test_parses_one_matching_target_and_file(symbol: Symbol, manifest) -> None:
    artifact = parse_preserve_abi_response(response(symbol), symbol, manifest)
    assert artifact.path == symbol.output_path
    assert artifact.address == symbol.address
    assert artifact.source.startswith("int fn")


@pytest.mark.parametrize(
    "raw",
    [
        "// FILE: unit/fn.cpp\nint fn() {}",
        "// TARGET: 0x401000\n// TARGET: 0x401000\n// FILE: unit/fn.cpp\nint fn() {}",
        "// TARGET: 0x402000\n// FILE: unit/fn.cpp\nint fn() {}",
        "// TARGET: 0x401000\n// FILE: unit/fn.cpp\n",
        "// TARGET: 0x401000\n// FILE: unit/other.cpp\nint fn() {}",
        "// TARGET: 0x401000\n// FILE: ../fn.cpp\nint fn() {}",
        "// TARGET: 0x401000\n// FILE: /unit/fn.cpp\nint fn() {}",
        "// TARGET: 0x401000\n// FILE: C:/unit/fn.cpp\nint fn() {}",
        "// TARGET: 0x401000\n// FILE: unit\\fn.cpp\nint fn() {}",
        "// TARGET: 0x401000\n// FILE: unit/fn.h\nint fn() {}",
        "explanation\n// TARGET: 0x401000\n// FILE: unit/fn.cpp\nint fn() {}",
        "// TARGET: 0x401000\n// FILE: unit/fn.cpp\nint fn() {}\n// FILE: unit/extra.cpp\nint x;",
    ],
)
def test_rejects_invalid_response(raw: str, symbol: Symbol, manifest) -> None:
    with pytest.raises(ManifestBoundTransformError):
        parse_preserve_abi_response(raw, symbol, manifest)


def test_rejects_symbol_not_in_verified_manifest(symbol: Symbol) -> None:
    other = Symbol(0x402000, "other", "void other()", CallingConvention.CDECL, "unit/other.cpp")
    manifest = manifest_from_symbols(version="1.0.0", architecture=Architecture.X86, pointer_size=4, symbols=[other])
    with pytest.raises(ManifestBoundTransformError):
        build_preserve_abi_prompt(symbol, "void fn() {}", manifest)


def test_does_not_claim_abi_or_behavior_verification(symbol: Symbol, manifest) -> None:
    artifact = parse_preserve_abi_response(response(symbol), symbol, manifest)
    assert artifact.source


class Provider:
    def __init__(self, text: str):
        self.text = text
        self.calls = 0

    def send(self, messages):
        self.calls += 1
        return self.text


def _cfg(tmp_path):
    return SimpleNamespace(
        input=SimpleNamespace(decompiled_dir=str(tmp_path / "src")),
        output=SimpleNamespace(
            work_dir=str(tmp_path / "work"),
            target_dir=str(tmp_path / "out"),
            compiler="g++",
            compiler_flags="-std=c++23 -c",
            decls_header=None,
        ),
    )


def _verified(manifest):
    return VerifiedContract(manifest, Path("manifest.json"), "a" * 64, manifest.sha256_hash)


def test_integration_real_object_and_atomic_unit(tmp_path, symbol, manifest):
    cfg = _cfg(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "0x401000__fn.cpp").write_text("int fn(int x) { return x; }", encoding="utf-8")
    provider = Provider(response(symbol))

    def compiler(source, obj, _cfg):
        obj.write_bytes(b"real-object")
        return True, "", "fake -c -o object"

    result = run_manifest_bound_transform(
        cfg, None, _verified(manifest), "0x401000", run_id="r1", provider=provider, compile_fn=compiler
    )
    assert result.successful
    assert result.compile_verdict is ManifestBoundVerdict.COMPILE_PASS
    assert provider.calls == 1
    unit = tmp_path / "out" / ".manifest-bound" / "r1" / "0x401000"
    assert (unit / "unit/fn.cpp").exists()
    assert (unit / "fn.o").read_bytes() == b"real-object"


def test_integration_compile_failure_does_not_publish(tmp_path, symbol, manifest):
    cfg = _cfg(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "0x401000__fn.cpp").write_text("int fn(int x) { return x; }", encoding="utf-8")
    provider = Provider(response(symbol))
    result = run_manifest_bound_transform(
        cfg,
        None,
        _verified(manifest),
        0x401000,
        provider=provider,
        compile_fn=lambda *_: (False, "error", "compiler"),
    )
    assert result.verdict is ManifestBoundVerdict.COMPILE_FAIL
    assert not (tmp_path / "out").exists()


def test_integration_invalid_source_is_before_provider(tmp_path, symbol, manifest):
    cfg = _cfg(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "0x4010000__wrong.cpp").write_text("int x;", encoding="utf-8")
    provider = Provider("")
    with pytest.raises(ManifestBoundTransformError):
        run_manifest_bound_transform(cfg, None, _verified(manifest), 0x401000, provider=provider)
    assert provider.calls == 0


def test_integration_no_persist_skips_compiler(tmp_path, symbol, manifest):
    cfg = _cfg(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "0x401000__fn.cpp").write_text("int fn(int x) { return x; }", encoding="utf-8")
    called = False

    def compiler(*_):
        nonlocal called
        called = True
        raise AssertionError("compiler must not run")

    result = run_manifest_bound_transform(
        cfg,
        None,
        _verified(manifest),
        0x401000,
        persist=False,
        provider=Provider(response(symbol)),
        compile_fn=compiler,
    )
    assert result.verdict is ManifestBoundVerdict.SKIPPED_COMPILE
    assert not result.compiles and not called
    assert not (tmp_path / "work").exists()


def test_integration_runs_are_isolated_and_historical_units_remain(tmp_path, symbol, manifest):
    cfg = _cfg(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "0x401000__fn.cpp").write_text("int fn(int x) { return x; }", encoding="utf-8")

    def compiler(source, obj, _cfg):
        obj.write_bytes(source.read_bytes())
        return True, "", "compiler"

    first = run_manifest_bound_transform(
        cfg,
        None,
        _verified(manifest),
        0x401000,
        run_id="history-1",
        provider=Provider(response(symbol)),
        compile_fn=compiler,
    )
    second = run_manifest_bound_transform(
        cfg,
        None,
        _verified(manifest),
        0x401000,
        run_id="history-2",
        provider=Provider(response(symbol)),
        compile_fn=compiler,
    )
    assert first.successful and second.successful
    assert (tmp_path / "out/.manifest-bound/history-1/0x401000").exists()
    assert (tmp_path / "out/.manifest-bound/history-2/0x401000").exists()
    # The run root is never removed; only each owned run directory is cleaned.
    assert (tmp_path / "work/run").exists()


def test_foreign_run_under_run_root_survives_cleanup(tmp_path, symbol, manifest):
    cfg = _cfg(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "0x401000__fn.cpp").write_text("int fn(int x) { return x; }", encoding="utf-8")
    foreign = tmp_path / "work/run/foreign-run/build/staging/other"
    foreign.mkdir(parents=True)
    marker = foreign / "in-flight.marker"
    marker.write_text("keep", encoding="utf-8")

    def compiler(source, obj, _cfg):
        obj.write_bytes(source.read_bytes())
        return True, "", "compiler"

    result = run_manifest_bound_transform(
        cfg,
        None,
        _verified(manifest),
        0x401000,
        run_id="owned-run",
        provider=Provider(response(symbol)),
        compile_fn=compiler,
    )
    assert result.successful
    assert marker.read_text(encoding="utf-8") == "keep"


def test_publication_revalidation_rejects_substitution_without_os_symlinks(tmp_path, symbol, manifest, monkeypatch):
    cfg = _cfg(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "0x401000__fn.cpp").write_text("int fn(int x) { return x; }", encoding="utf-8")
    # Model a reparse substitution after mkdir without requiring Windows
    # symlink privileges: final_unit.resolve() starts resolving outside target.
    original_mkdir = Path.mkdir
    original_resolve = Path.resolve
    created = False

    def mkdir(path, *args, **kwargs):
        nonlocal created
        result = original_mkdir(path, *args, **kwargs)
        if ".manifest-bound" in path.parts:
            created = True
        return result

    def resolve(path, *args, **kwargs):
        if created and path.name == "0x401000" and ".manifest-bound" in path.parts:
            return (tmp_path / "outside").absolute()
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", mkdir)
    monkeypatch.setattr(Path, "resolve", resolve)

    def compiler(source, obj, _cfg):
        obj.write_bytes(source.read_bytes())
        return True, "", "compiler"

    with pytest.raises(ManifestBoundTransformError):
        run_manifest_bound_transform(
            cfg,
            None,
            _verified(manifest),
            0x401000,
            run_id="substitution",
            provider=Provider(response(symbol)),
            compile_fn=compiler,
        )
    assert not (tmp_path / "out/.manifest-bound/substitution/0x401000").exists()


def test_integration_publication_rollback_keeps_staging(tmp_path, symbol, manifest):
    cfg = _cfg(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "0x401000__fn.cpp").write_text("int fn(int x) { return x; }", encoding="utf-8")
    existing = tmp_path / "out/.manifest-bound/r1/0x401000"
    existing.mkdir(parents=True)

    def compiler(source, obj, _cfg):
        obj.write_bytes(b"o")
        return True, "", "compiler"

    with pytest.raises(ManifestBoundTransformError):
        run_manifest_bound_transform(
            cfg,
            None,
            _verified(manifest),
            0x401000,
            run_id="r1",
            provider=Provider(response(symbol)),
            compile_fn=compiler,
        )
    assert existing.exists()
    assert (tmp_path / "work/run/r1").exists()


def test_integration_invalid_utf8_and_symlink_are_before_provider(tmp_path, symbol, manifest):
    cfg = _cfg(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "0x401000__fn.cpp").write_bytes(b"\xff")
    provider = Provider("")
    with pytest.raises(ManifestBoundTransformError):
        run_manifest_bound_transform(cfg, None, _verified(manifest), 0x401000, provider=provider)
    assert provider.calls == 0
    if hasattr((tmp_path / "out").parent, "symlink_to"):
        real = tmp_path / "real-out"
        real.mkdir()
        link = tmp_path / "out-link"
        try:
            link.symlink_to(real, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks unavailable")
        cfg.output.target_dir = str(link)
        (src / "0x401000__fn.cpp").write_text("int fn(int x) { return x; }", encoding="utf-8")
        valid = Provider(response(symbol))
        with pytest.raises(ManifestBoundTransformError):
            run_manifest_bound_transform(cfg, None, _verified(manifest), 0x401000, provider=valid)


def test_compile_real_honors_o_and_emits_non_text_object(tmp_path):
    tool = tmp_path / "compiler.py"
    tool.write_text(
        "import pathlib,sys\n"
        "args=sys.argv[1:]\n"
        "out=pathlib.Path(args[args.index('-o')+1])\n"
        "out.write_bytes(b'OBJ\\0REAL')\n",
        encoding="utf-8",
    )
    source = tmp_path / "x.cpp"
    source.write_text("int x;", encoding="utf-8")
    obj = tmp_path / "x.o"
    cfg = SimpleNamespace(output=SimpleNamespace(compiler=sys.executable, compiler_flags=f'"{tool}"'))
    passed, _log, command = _compile_real(source, obj, cfg)
    assert passed and obj.read_bytes() == b"OBJ\0REAL"
    assert "-o" in command


def test_integration_invalid_verified_contract_and_run_id(tmp_path, symbol, manifest):
    cfg = _cfg(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "0x401000__fn.cpp").write_text("int fn(int x) { return x; }", encoding="utf-8")
    provider = Provider(response(symbol))
    with pytest.raises(ManifestBoundTransformError):
        run_manifest_bound_transform(cfg, None, object(), 0x401000, provider=provider)
    with pytest.raises(ManifestBoundTransformError):
        run_manifest_bound_transform(cfg, None, _verified(manifest), 0x401000, run_id="../escape", provider=provider)
    (tmp_path / "work/run/occupied").mkdir(parents=True)
    with pytest.raises(ManifestBoundTransformError):
        run_manifest_bound_transform(cfg, None, _verified(manifest), 0x401000, run_id="occupied", provider=provider)


def test_no_persist_provider_error_preserves_classification_and_usage(tmp_path, symbol, manifest):
    cfg = _cfg(tmp_path)

    class FailingProvider(Provider):
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_calls = 0

        def send(self, messages):
            self.total_prompt_tokens = 17
            self.total_completion_tokens = 5
            self.total_calls = 1
            raise RuntimeError("rate limited")

    src = tmp_path / "src"
    src.mkdir()
    (src / "0x401000__fn.cpp").write_text("int fn(int x) { return x; }", encoding="utf-8")
    result = run_manifest_bound_transform(
        cfg, None, _verified(manifest), 0x401000, persist=False, provider=FailingProvider("")
    )
    assert result.verdict is ManifestBoundVerdict.PROVIDER_ERROR
    assert result.provider_errors == 1
    assert result.usage == {"prompt_tokens": 17, "completion_tokens": 5, "total_calls": 1}


def test_no_persist_budget_exhausted_skips_provider(tmp_path, symbol, manifest):
    cfg = _cfg(tmp_path)
    cfg.optimization = SimpleNamespace(
        max_llm_calls_per_run=0, max_llm_tokens_per_run=1, max_compile_retry_calls_per_run=3
    )
    src = tmp_path / "src"
    src.mkdir()
    (src / "0x401000__fn.cpp").write_text("int fn(int x) { return x; }", encoding="utf-8")
    provider = Provider(response(symbol))
    result = run_manifest_bound_transform(cfg, None, _verified(manifest), 0x401000, persist=False, provider=provider)
    assert result.verdict is ManifestBoundVerdict.BUDGET_EXCEEDED
    assert provider.calls == 0
    assert result.budget["exceeded"] is True
