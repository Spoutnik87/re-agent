"""Shared stagnation detection for fix loops.

Prevents infinite fix loops by detecting when consecutive rounds
produce no improvement (same verdict, same or more issues).
"""

from __future__ import annotations

import logging

from re_agent.reverse.core.models import CheckerVerdict, ObjectiveVerdict, Verdict

logger = logging.getLogger(__name__)


class StagnationTracker:
    """Detects when a fix loop has stopped making progress.

    After 2 consecutive rounds with no improvement (same verdict,
    same-or-worse issue count), the loop should terminate.
    """

    def __init__(self) -> None:
        self.round = 0
        self.last_verdict: Verdict | None = None
        self.last_issue_count: int = 999
        self.last_change_round: int = 0

    def update(self, cv: CheckerVerdict) -> bool:
        """Record a new round result and return True if the loop stagnated."""
        self.round += 1
        current_count = len(cv.issues)
        if cv.verdict == self.last_verdict and current_count >= self.last_issue_count:
            if self.round - self.last_change_round >= 2:
                return True
        else:
            self.last_change_round = self.round
            self.last_verdict = cv.verdict
            self.last_issue_count = current_count
        return False

    @staticmethod
    def is_pass(
        cv: CheckerVerdict,
        ov: ObjectiveVerdict | None = None,
    ) -> bool:
        """Return True if both checker and optional objective verifier pass."""
        return cv.verdict == Verdict.PASS and (ov is None or ov.verdict != Verdict.FAIL)
