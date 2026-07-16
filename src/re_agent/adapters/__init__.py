"""Public Release 5 generic adapter boundary."""

from re_agent.adapters.contracts import (
    REQUEST_PROTOCOL,
    RESULT_PROTOCOL,
    AdapterAttachment,
    AdapterCommand,
    AdapterRequest,
    AdapterResult,
)
from re_agent.adapters.execution import (
    AdapterEvidence,
    AdapterExecution,
    execute_adapter,
    execute_adapter_with_evidence,
)

__all__ = [
    "REQUEST_PROTOCOL",
    "RESULT_PROTOCOL",
    "AdapterAttachment",
    "AdapterCommand",
    "AdapterRequest",
    "AdapterResult",
    "AdapterEvidence",
    "AdapterExecution",
    "execute_adapter",
    "execute_adapter_with_evidence",
]
