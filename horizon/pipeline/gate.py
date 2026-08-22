"""Step 2 — fake news gate.

Phase 1 is source-credibility only: no ML model, no content inspection. The
`GateStrategy` protocol below is the extension point for the future
WangchanBERTa classifier — add a new strategy class, register it in
`build_gate()`, and leave `CredibilityGate` untouched so the credibility floor
still applies.

**Do not implement the classifier here yet.**
"""

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..config import get_settings


@dataclass(frozen=True)
class GateDecision:
    passed: bool
    reason: str
    #: Terminal `raw_articles.status` when `passed` is False.
    status: str = "dropped_lowcred"


@runtime_checkable
class GateStrategy(Protocol):
    """Decides whether an article may become an event.

    Implementations must be pure and fast — this runs before any LLM call, on
    every article. Anything needing inference belongs in a strategy that can be
    swapped in without touching the worker.
    """

    name: str

    def evaluate(self, *, title: str, body: str, credibility_weight: float) -> GateDecision: ...


class CredibilityGate:
    """Phase 1 strategy: drop anything from a source below the credibility floor."""

    name = "credibility"

    def __init__(self, min_credibility: float | None = None) -> None:
        self.min_credibility = (
            get_settings().gate_min_credibility
            if min_credibility is None
            else min_credibility
        )

    def evaluate(self, *, title: str, body: str, credibility_weight: float) -> GateDecision:
        if credibility_weight < self.min_credibility:
            return GateDecision(
                passed=False,
                reason=f"credibility {credibility_weight:.2f} < {self.min_credibility:.2f}",
                status="dropped_lowcred",
            )
        return GateDecision(passed=True, reason="ok")


# ── Extension point ──────────────────────────────────────────────────────────
# Phase 2 of the gate adds a content classifier (WangchanBERTa). It will slot in
# as a second strategy composed with CredibilityGate — the credibility floor is
# never bypassed.
#
#   class WangchanBertaGate:
#       name = "wangchanberta"
#       def evaluate(self, *, title, body, credibility_weight) -> GateDecision: ...
#
# Register it here and select via an env var; do not branch inside the worker.
def build_gate() -> GateStrategy:
    return CredibilityGate()
