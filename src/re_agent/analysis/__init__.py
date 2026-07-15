"""Lifecycle adapters for obtaining generic analysis snapshots."""

from re_agent.analysis.ghidra import GhidraLifecycleBackend, GhidraLifecycleError
from re_agent.analysis.lifecycle import AnalysisLifecycleBackend, BackendFingerprint, BackendHealth
from re_agent.analysis.offline_export import OfflineExportBackend, OfflineExportError

__all__ = [
    "AnalysisLifecycleBackend",
    "BackendFingerprint",
    "BackendHealth",
    "GhidraLifecycleBackend",
    "GhidraLifecycleError",
    "OfflineExportBackend",
    "OfflineExportError",
]
