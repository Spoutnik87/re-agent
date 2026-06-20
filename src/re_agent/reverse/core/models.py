"""Core data models for re-agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

# ---------------------------------------------------------------------------
# Target identification
# ---------------------------------------------------------------------------


@dataclass
class FunctionTarget:
    """Identifies a single function to reverse."""

    address: str
    class_name: str
    function_name: str
    caller_count: int = 0


# ---------------------------------------------------------------------------
# Verdict / status enums
# ---------------------------------------------------------------------------


class Verdict(Enum):
    """Checker verdict for a reversal attempt."""

    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


class ParityStatus(Enum):
    """Static parity triage status."""

    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


# ---------------------------------------------------------------------------
# Checker results
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """A single parity finding."""

    level: str  # "red", "yellow", or "info"
    reason: str


@dataclass
class CheckerVerdict:
    """Structured result from the checker agent."""

    verdict: Verdict
    summary: str
    issues: list[str] = field(default_factory=list)
    fix_instructions: list[str] = field(default_factory=list)
    naming_issues_explicit: bool = False
    affected_variables: list[str] = field(default_factory=list)


@dataclass
class ObjectiveVerdict:
    """Structured result from conservative non-LLM verification."""

    verdict: Verdict
    summary: str
    findings: list[str] = field(default_factory=list)


@dataclass
class ReversalResult:
    """Complete result of reversing one function."""

    target: FunctionTarget
    code: str
    checker_verdict: CheckerVerdict | None = None
    objective_verdict: ObjectiveVerdict | None = None
    parity_status: ParityStatus | None = None
    parity_findings: list[Finding] = field(default_factory=list)
    rounds_used: int = 0
    success: bool = False
    block_code: dict[str, str] | None = None
    var_mapping: str | None = None


# ---------------------------------------------------------------------------
# Ghidra / decompiler data
# ---------------------------------------------------------------------------


@dataclass
class DecompileResult:
    """Parsed output from a decompiler invocation."""

    address: str
    name: str
    signature: str
    decompiled: str
    raw_output: str
    callers: int | None = None
    callees: int | None = None


@dataclass
class XRef:
    """A single cross-reference entry."""

    address: str
    name: str
    ref_type: str


@dataclass
class FunctionEntry:
    """A function entry from the decompiler's function list."""

    address: str
    name: str
    class_name: str = ""
    caller_count: int = 0


@dataclass
class StructField:
    """A single field within a struct definition."""

    name: str
    offset: int
    type_str: str
    size: int


@dataclass
class StructDef:
    """A struct/class definition from the decompiler."""

    name: str
    size: int
    fields: list[StructField] = field(default_factory=list)


@dataclass
class EnumValue:
    """A single value within an enum definition."""

    name: str
    value: int


@dataclass
class EnumDef:
    """An enum definition from the decompiler."""

    name: str
    values: list[EnumValue] = field(default_factory=list)


@dataclass
class AsmResult:
    """Parsed assembly listing for a function."""

    address: str
    instructions: str
    instruction_count: int
    call_count: int
    has_fp_sensitive: bool


# ---------------------------------------------------------------------------
# Source analysis data
# ---------------------------------------------------------------------------


@dataclass
class SourceMatch:
    """Parsed source function body with analysis metrics."""

    path: str
    line: int
    body: str
    body_no_comments: str
    body_lines: int
    call_count: int
    plugin_call_count: int
    non_plugin_call_count: int
    control_flow_count: int
    has_stub_marker: bool
    has_fp_token: bool
    is_inline_internal_forwarder: bool


@dataclass
class GhidraData:
    """Aggregated Ghidra analysis data for a function."""

    decompile_ok: bool = False
    decompile_error: str | None = None
    callers: int | None = None
    callees: int | None = None
    param_offsets: int = 0
    decompile_has_nan: bool = False
    asm_ok: bool = False
    asm_error: str | None = None
    asm_instruction_count: int = 0
    asm_call_count: int = 0
    asm_has_fp_sensitive: bool = False
    refs_call_count: int = 0
    refs_global_rw_count: int = 0
    used_containing_fallback: bool = False
    resolved_address: str | None = None


# ---------------------------------------------------------------------------
# Hook registry
# ---------------------------------------------------------------------------


@dataclass
class HookEntry:
    """A single hook from the hooks CSV registry."""

    class_path: str
    fn_name: str
    address: str
    reversed: bool
    locked: bool
    is_virtual: bool

    @property
    def class_name(self) -> str:
        """Extract the class name from the class path."""
        return self.class_path.split("/")[-1]

    @property
    def symbol(self) -> str:
        """Return the fully-qualified symbol name."""
        return f"{self.class_name}::{self.fn_name}"


# ---------------------------------------------------------------------------
# Semantic parity rules
# ---------------------------------------------------------------------------


@dataclass
class SemanticRule:
    """A semantic parity rule loaded from a JSON rules file."""

    id: str
    reason: str
    severity: str  # "red", "yellow", or "info"
    addresses: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    source_all_of: list[str] = field(default_factory=list)
    source_any_of: list[str] = field(default_factory=list)
    source_none_of: list[str] = field(default_factory=list)


@dataclass
class ManualCheckEntry:
    """A manually-verified parity check entry."""

    line: int
    note: str


# ---------------------------------------------------------------------------
# Pipeline profile — maps classification to pipeline execution flags
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PipelineProfile:
    """Execution profile derived from function classification.

    Controls which pipeline steps run for a given function type,
    allowing trivial functions to skip expensive steps.

    few_shot_max_examples: Max examples to inject from FewShotBuilder (0 = disable few-shot).
    """

    max_rounds: int
    enable_phase1: bool
    inject_source_context: bool
    inject_few_shot: bool
    use_objective_verifier: bool
    few_shot_max_examples: int = 0  # 0 = disable; non-zero = pass to find_similar()


_PROFILES: dict[str, PipelineProfile] = {
    "leaf": PipelineProfile(
        max_rounds=1,
        enable_phase1=False,
        inject_source_context=False,
        inject_few_shot=False,
        use_objective_verifier=False,
        few_shot_max_examples=0,
    ),
    "getter-setter": PipelineProfile(
        max_rounds=1,
        enable_phase1=False,
        inject_source_context=False,
        inject_few_shot=False,
        use_objective_verifier=False,
        few_shot_max_examples=0,
    ),
    "win32-api": PipelineProfile(
        max_rounds=2,
        enable_phase1=True,
        inject_source_context=False,
        inject_few_shot=True,
        use_objective_verifier=True,
        few_shot_max_examples=2,
    ),
    "vtable-heavy": PipelineProfile(
        max_rounds=5,
        enable_phase1=True,
        inject_source_context=True,
        inject_few_shot=True,
        use_objective_verifier=True,
        few_shot_max_examples=3,
    ),
    # max_rounds=2: complex functions need a fix cycle, but fewer rounds than "general"
    # to cap token cost on deeply nested logic that often stagnates after 2 rounds anyway.
    "complex-state-machine": PipelineProfile(
        max_rounds=2,
        enable_phase1=True,
        inject_source_context=True,
        inject_few_shot=True,
        use_objective_verifier=True,
        few_shot_max_examples=2,
    ),
    "general": PipelineProfile(
        max_rounds=4,
        enable_phase1=True,
        inject_source_context=True,
        inject_few_shot=True,
        use_objective_verifier=True,
        few_shot_max_examples=2,
    ),
}

_GENERAL_PROFILE = _PROFILES["general"]


def profile_for(classification: str) -> PipelineProfile:
    """Return the PipelineProfile for a given pre_classify() result."""
    return _PROFILES.get(classification, _GENERAL_PROFILE)
