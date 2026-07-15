"""Small, contract-bound primitive for a single preserve-ABI transform.

This module deliberately does not call an LLM or prove ABI correctness.  It
only creates the constrained request and validates the response envelope.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from re_agent.contracts.model import AbiManifest, Symbol
from re_agent.contracts.runtime import VerifiedContract
from re_agent.toolchain.activation import VerifiedCommand, verify_command

__all__ = [
    "ManifestBoundTransformError",
    "ManifestBoundPrompt",
    "ManifestBoundArtifact",
    "ManifestBoundVerdict",
    "ManifestBoundResult",
    "build_preserve_abi_prompt",
    "parse_preserve_abi_response",
    "run_manifest_bound_transform",
]


class ManifestBoundTransformError(ValueError):
    """The request or response is not a valid manifest-bound envelope."""


class ManifestBoundVerdict(StrEnum):
    MANIFEST_BOUND = "MANIFEST_BOUND"
    COMPILE_PASS = "COMPILE_PASS"
    SKIPPED_COMPILE = "SKIPPED_COMPILE"
    COMPILE_FAIL = "COMPILE_FAIL"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    VALIDATION_ERROR = "NO_OUTPUT"


@dataclass(frozen=True, slots=True)
class ManifestBoundPrompt:
    system: str
    user: str


@dataclass(frozen=True, slots=True)
class ManifestBoundArtifact:
    address: int
    path: str
    source: str


@dataclass(frozen=True, slots=True)
class ManifestBoundResult:
    verdict: ManifestBoundVerdict
    address: int
    path: str
    compiler_log: str = ""
    compiles: bool = False
    compile_verdict: ManifestBoundVerdict | None = None
    usage: dict[str, int] = None  # type: ignore[assignment]
    budget: dict[str, object] = None  # type: ignore[assignment]
    provider_errors: int = 0
    error: str = ""

    @property
    def successful(self) -> bool:
        return (
            self.verdict is ManifestBoundVerdict.MANIFEST_BOUND
            and self.compile_verdict is ManifestBoundVerdict.COMPILE_PASS
            and self.compiles
        )

    def __post_init__(self) -> None:
        if self.usage is None:
            object.__setattr__(self, "usage", {"prompt_tokens": 0, "completion_tokens": 0, "total_calls": 0})
        if self.budget is None:
            object.__setattr__(self, "budget", {})


def _contract_symbol(symbol: Symbol, manifest: AbiManifest) -> None:
    if not isinstance(manifest, AbiManifest):
        raise ManifestBoundTransformError("manifest must be an already verified AbiManifest")
    if symbol not in manifest.symbols:
        raise ManifestBoundTransformError("symbol is not an entry in the verified manifest")


def build_preserve_abi_prompt(
    symbol: Symbol,
    source: str,
    verified_manifest: AbiManifest,
) -> ManifestBoundPrompt:
    """Build a prompt containing only the five manifest identity fields."""
    _contract_symbol(symbol, verified_manifest)
    if not source.strip():
        raise ManifestBoundTransformError("source must be non-empty")

    identity = (
        f"address: 0x{symbol.address:x}\n"
        f"name: {symbol.name}\n"
        f"signature: {symbol.signature}\n"
        f"calling_convention: {symbol.calling_convention.value}\n"
        f"output_path: {symbol.output_path}"
    )
    system = (
        "Transform exactly one target while preserving its declared ABI.\n"
        "Return exactly one TARGET marker followed by exactly one FILE marker.\n"
        "Return one non-empty .cpp source file and no prose, headers, helpers, "
        "stubs, additional targets, renames, or other artifacts."
    )
    user = f"TARGET CONTRACT\n{identity}\n\nSOURCE\n{source}"
    return ManifestBoundPrompt(system=system, user=user)


_TARGET_RE = re.compile(r"^//\s*TARGET:\s*(?:(?:\d+)\s+)?(0x[0-9a-fA-F]+)\s*$", re.MULTILINE)
_FILE_RE = re.compile(r"^//\s*FILE:\s*(.*)$", re.MULTILINE)
_FENCE_RE = re.compile(r"^\s*```[^\n]*\n?|\n?\s*```\s*$", re.MULTILINE)


def _safe_relative_cpp(path: str) -> None:
    if not path or "\\" in path or path.startswith("/"):
        raise ManifestBoundTransformError("artifact path is not a relative POSIX path")
    if re.match(r"^[A-Za-z]:", path) or path.startswith("//"):
        raise ManifestBoundTransformError("artifact path is absolute")
    parts = PurePosixPath(path).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ManifestBoundTransformError("artifact path contains traversal")
    if not path.endswith(".cpp"):
        raise ManifestBoundTransformError("artifact is not a .cpp file")


def parse_preserve_abi_response(
    response: str,
    symbol: Symbol,
    verified_manifest: AbiManifest,
) -> ManifestBoundArtifact:
    """Parse one strict response and bind it to ``symbol``.

    This validates identity and artifact shape only; it does not inspect ABI,
    types, callees, or generated behavior.
    """
    _contract_symbol(symbol, verified_manifest)
    targets = list(_TARGET_RE.finditer(response))
    files = list(_FILE_RE.finditer(response))
    if len(targets) != 1:
        raise ManifestBoundTransformError("response must contain exactly one TARGET")
    if len(files) != 1:
        raise ManifestBoundTransformError("response must contain exactly one FILE")
    target = int(targets[0].group(1), 16)
    if target != symbol.address:
        raise ManifestBoundTransformError("TARGET address does not match the manifest symbol")
    path = files[0].group(1).strip()
    _safe_relative_cpp(path)
    if path != symbol.output_path:
        raise ManifestBoundTransformError("artifact path does not exactly match output_path")

    # The protocol is marker-only: no extra artifact or explanatory prose.
    before = response[: targets[0].start()]
    between = response[targets[0].end() : files[0].start()]
    content = response[files[0].end() :]
    content = _FENCE_RE.sub("", content).strip()
    if before.strip(" \t\r\n`") or between.strip(" \t\r\n`"):
        raise ManifestBoundTransformError("response contains text outside the one target")
    if not content:
        raise ManifestBoundTransformError(".cpp artifact is empty")
    return ManifestBoundArtifact(address=target, path=path, source=content)


def _reject_symlink_components(path: Path) -> None:
    """Reject every existing symlink in a publication path, lexically."""
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            if current.is_symlink():
                raise ManifestBoundTransformError(f"publication path contains a symlink: {current}")
        except OSError as exc:
            raise ManifestBoundTransformError(f"cannot inspect publication path: {current}") from exc


def _revalidate_publication(target_root: Path, final_parent: Path, final_unit: Path) -> None:
    _reject_symlink_components(target_root)
    _reject_symlink_components(final_parent)
    _reject_symlink_components(final_unit)
    try:
        resolved = final_unit.resolve(strict=False)
    except OSError as exc:
        raise ManifestBoundTransformError("cannot resolve publication path") from exc
    if not resolved.is_relative_to(target_root.absolute()):
        raise ManifestBoundTransformError("publication path escapes target directory")


def run_manifest_bound_transform(
    build_cfg: Any,
    llm_cfg: Any,
    verified_contract: Any,
    address: str | int,
    *,
    run_id: str = "",
    persist: bool = True,
    provider: Any = None,
    compile_fn: Callable[[Path, Path, Any], tuple[bool, str, str]] | None = None,
    verified_compile_command: VerifiedCommand | None = None,
) -> ManifestBoundResult:
    """Execute the bounded, single-symbol preserve-ABI transform.

    This is intentionally an integration helper: the primitive above remains
    free of provider, filesystem, and compiler concerns.  All durable files
    are prepared in a private staging directory and published only after the
    response and compile gate have succeeded.
    """
    if compile_fn is not None and verified_compile_command is not None:
        raise ManifestBoundTransformError("compile_fn and verified_compile_command are mutually exclusive")
    if not isinstance(verified_contract, VerifiedContract):
        raise ManifestBoundTransformError("contracts.verified_manifest must be a VerifiedContract")
    manifest = verified_contract.manifest
    if not isinstance(manifest, AbiManifest):
        raise ManifestBoundTransformError("verified contract does not contain an AbiManifest")
    digest_re = re.compile(r"^[0-9a-fA-F]{64}$")
    if not digest_re.fullmatch(verified_contract.raw_sha256) or not digest_re.fullmatch(
        verified_contract.canonical_sha256
    ):
        raise ManifestBoundTransformError("verified contract hashes must be SHA-256 digests")
    if verified_contract.canonical_sha256.lower() != manifest.sha256_hash.lower():
        raise ManifestBoundTransformError("verified canonical hash does not match manifest")
    try:
        numeric_address = int(address, 0) if isinstance(address, str) else int(address)
    except (TypeError, ValueError) as exc:
        raise ManifestBoundTransformError("--address is not a valid integer address") from exc
    symbols = [s for s in manifest.symbols if s.address == numeric_address]
    if len(symbols) != 1:
        raise ManifestBoundTransformError(
            f"verified manifest must contain exactly one symbol for 0x{numeric_address:x}"
        )
    symbol = symbols[0]

    source_dir = Path(build_cfg.input.decompiled_dir)
    filename_re = re.compile(r"^0x([0-9a-fA-F]+)(?:__(?:[^/\\]+))?\.cpp$")
    candidates = []
    if source_dir.is_dir():
        for candidate in source_dir.iterdir():
            match = filename_re.fullmatch(candidate.name)
            if match and int(match.group(1), 16) == numeric_address:
                candidates.append(candidate)
    if len(candidates) != 1:
        raise ManifestBoundTransformError(
            f"expected exactly one source candidate for 0x{numeric_address:x}, found {len(candidates)}"
        )
    candidate = candidates[0]
    if candidate.is_symlink() or not candidate.is_file():
        raise ManifestBoundTransformError("source candidate is not a regular file")
    try:
        source = candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestBoundTransformError("source candidate is not strict UTF-8") from exc

    prompt = build_preserve_abi_prompt(symbol, source, manifest)
    if provider is None:
        from re_agent.llm.registry import create_provider

        provider = create_provider(llm_cfg)
    from re_agent.llm.protocol import Message, get_usage

    start_usage = get_usage(provider)
    optimization = getattr(build_cfg, "optimization", None)
    max_calls = int(getattr(optimization, "max_llm_calls_per_run", 8))
    max_tokens = int(getattr(optimization, "max_llm_tokens_per_run", 150000))
    budget = {
        "calls_remaining": max_calls,
        "tokens_remaining": max_tokens,
        "compile_retry_calls_remaining": int(getattr(optimization, "max_compile_retry_calls_per_run", 3)),
        "exceeded": False,
        "exceeded_reason": "",
    }
    if max_calls <= 0 or max_tokens <= 0:
        budget["exceeded"] = True
        budget["exceeded_reason"] = "LLM budget exhausted before provider call"
        return ManifestBoundResult(
            ManifestBoundVerdict.BUDGET_EXCEEDED,
            numeric_address,
            symbol.output_path,
            budget=budget,
            error=str(budget["exceeded_reason"]),
        )
    try:
        response = provider.send([Message("system", prompt.system), Message("user", prompt.user)])
    except Exception as exc:
        end_usage = get_usage(provider)
        usage = _usage_delta(start_usage, end_usage)
        budget["calls_remaining"] = max(0, max_calls - usage["total_calls"])
        budget["tokens_remaining"] = max(0, max_tokens - usage["prompt_tokens"] - usage["completion_tokens"])
        return ManifestBoundResult(
            ManifestBoundVerdict.PROVIDER_ERROR,
            numeric_address,
            symbol.output_path,
            usage=usage,
            budget=budget,
            provider_errors=1,
            error=str(exc),
        )
    end_usage = get_usage(provider)
    usage = _usage_delta(start_usage, end_usage)
    budget["calls_remaining"] = max(0, max_calls - usage["total_calls"])
    budget["tokens_remaining"] = max(0, max_tokens - usage["prompt_tokens"] - usage["completion_tokens"])
    if usage["total_calls"] > max_calls or usage["prompt_tokens"] + usage["completion_tokens"] > max_tokens:
        budget["exceeded"] = True
        budget["exceeded_reason"] = "LLM budget exhausted after provider call"
        return ManifestBoundResult(
            ManifestBoundVerdict.BUDGET_EXCEEDED,
            numeric_address,
            symbol.output_path,
            usage=usage,
            budget=budget,
            error=str(budget["exceeded_reason"]),
        )
    try:
        artifact = parse_preserve_abi_response(response, symbol, manifest)
    except ManifestBoundTransformError as exc:
        return ManifestBoundResult(
            ManifestBoundVerdict.VALIDATION_ERROR,
            numeric_address,
            symbol.output_path,
            usage=usage,
            budget=budget,
            error=str(exc),
        )

    if not persist:
        return ManifestBoundResult(
            ManifestBoundVerdict.SKIPPED_COMPILE,
            numeric_address,
            artifact.path,
            "",
            False,
            usage=usage,
            budget=budget,
        )

    target = Path(build_cfg.output.target_dir)
    _reject_symlink_components(target)
    work = Path(build_cfg.output.work_dir)
    run_name = run_id or f"run-{uuid.uuid4().hex}"
    if not re.fullmatch(r"[A-Za-z0-9._-]+", run_name) or run_name in {".", ".."}:
        raise ManifestBoundTransformError("run_id must be a single safe path component")
    run_root = (work / "run").resolve()
    run_root.mkdir(parents=True, exist_ok=True)
    run_dir = run_root / run_name
    try:
        run_dir.mkdir(exist_ok=False)
    except FileExistsError as exc:
        raise ManifestBoundTransformError("run_id is already in use") from exc
    staging_root = run_dir / "build" / "staging"
    staging_root.mkdir(parents=True, exist_ok=False)
    staging = staging_root / uuid.uuid4().hex
    staging.mkdir(exist_ok=False)
    target_root = target.absolute()
    final_parent = target_root / ".manifest-bound" / run_name
    final_unit = final_parent / f"0x{numeric_address:x}"
    if not final_unit.resolve().is_relative_to(target_root):
        raise ManifestBoundTransformError("publication path escapes target directory")
    _revalidate_publication(target_root, final_parent, final_unit)
    staged_source = staging / artifact.path
    staged_source.parent.mkdir(parents=True, exist_ok=True)
    staged_source.write_text(artifact.source, encoding="utf-8")
    staged_object = staging / (Path(artifact.path).stem + ".o")
    if verified_compile_command is not None:
        compiles, compiler_log, command = _compile_verified(staged_source, staged_object, verified_compile_command)
    else:
        if compile_fn is None:
            compile_fn = _compile_real
        compiles, compiler_log, command = compile_fn(staged_source, staged_object, build_cfg)
    if not compiles or not staged_object.is_file() or staged_object.stat().st_size == 0:
        shutil.rmtree(run_dir, ignore_errors=True)
        return ManifestBoundResult(
            ManifestBoundVerdict.COMPILE_FAIL,
            numeric_address,
            artifact.path,
            compiler_log,
            False,
            ManifestBoundVerdict.COMPILE_FAIL,
        )
    staged_log = staging / "compiler.log"
    staged_log.write_text(compiler_log, encoding="utf-8")
    provenance = {
        "address": f"0x{numeric_address:x}",
        "output_path": artifact.path,
        "verdicts": ["MANIFEST_BOUND", "COMPILE_PASS"],
        "manifest_sha256": verified_contract.canonical_sha256,
        "source_candidate": str(candidate),
        "source_sha256": _sha256(staged_source),
        "object_sha256": _sha256(staged_object),
        "command": command,
        "flags": getattr(build_cfg.output, "compiler_flags", ""),
    }
    staged_provenance = staging / "provenance.json"
    staged_provenance.write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")

    final_parent.mkdir(parents=True, exist_ok=True)
    try:
        # Revalidate after directory creation: this closes the substitution
        # window before the sole publication rename.
        _revalidate_publication(target_root, final_parent, final_unit)
        if final_unit.exists() or final_unit.is_symlink():
            raise ManifestBoundTransformError("publication unit already exists")
        os.replace(staging, final_unit)
        shutil.rmtree(run_dir, ignore_errors=True)
    except Exception:
        # Never remove an unpublished staging tree on a failed publication.
        raise
    return ManifestBoundResult(
        ManifestBoundVerdict.MANIFEST_BOUND,
        numeric_address,
        artifact.path,
        compiler_log,
        True,
        ManifestBoundVerdict.COMPILE_PASS,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _compile_verified(source: Path, object_path: Path, command: VerifiedCommand) -> tuple[bool, str, str]:
    """Compile using an immutable capability profile, never legacy defaults."""
    verify_command(command)
    argv = [*command.argv, "-o", str(object_path), str(source)]
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=60, shell=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc), json.dumps(argv)
    output = (completed.stdout or "") + (completed.stderr or "")
    return completed.returncode == 0, output, json.dumps(argv)


def _usage_delta(start: Any, end: Any) -> dict[str, int]:
    return {
        "prompt_tokens": max(0, (end.prompt_tokens or 0) - (start.prompt_tokens or 0)),
        "completion_tokens": max(0, (end.completion_tokens or 0) - (start.completion_tokens or 0)),
        "total_calls": max(0, (end.calls or 0) - (start.calls or 0)),
    }


def _compile_real(source: Path, obj: Path, cfg: Any) -> tuple[bool, str, str]:
    compiler = str(cfg.output.compiler)
    flags = shlex.split(str(cfg.output.compiler_flags))
    command = [compiler, *flags]
    decls = getattr(cfg.output, "decls_header", None)
    if decls:
        command.extend(["-include", str(decls), "-I", str(Path(decls).parent)])
    command.extend(["-o", str(obj), str(source)])
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc), " ".join(command)
    log = completed.stderr + completed.stdout
    return completed.returncode == 0, log, " ".join(command)
