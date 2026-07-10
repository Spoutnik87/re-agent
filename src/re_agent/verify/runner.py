from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class RunInputs:
    """One concrete invocation: positional args + initial memory snapshot."""

    args: tuple[int, ...] = ()
    memory: dict[str, bytes] = field(default_factory=dict)

    def key(self) -> tuple[object, ...]:
        return (self.args, tuple(sorted(self.memory.items())))


@dataclass
class RunResult:
    """Observable outcome of one invocation."""

    return_value: int
    writes: dict[str, bytes] = field(default_factory=dict)


@runtime_checkable
class FunctionRunner(Protocol):
    """Executes a single function for a given input vector."""

    def run(self, inputs: RunInputs) -> RunResult: ...
