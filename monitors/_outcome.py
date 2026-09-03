from __future__ import annotations

"""Per-run tally of a monitor's independent source units.

Every monitor polls N independent units -- EDGAR entities, RSS feeds, search
queries, scraped pages -- inside a per-unit ``try/except ... continue`` so one
dead source never kills the rest. That isolation had a blind spot: a run in
which EVERY unit failed still returned normally (with zero events), so the
orchestrator recorded ``last_run`` and the interval gate then suppressed the
monitor until the interval elapsed again. A source outage was therefore
indistinguishable from "nothing happened", and produced silent gaps in coverage.

``UnitTally`` makes that state observable. Monitors record one outcome per unit
they actually ATTEMPT, then call ``raise_if_total_failure`` before returning. A
total failure raises ``MonitorError``, which the orchestrator treats as "this
monitor did not run" and therefore does NOT advance ``last_run`` -- so the
monitor retries on the very next pass instead of waiting out its interval.

Only ATTEMPTED units are tallied. A unit skipped before any I/O (an empty feed
URL, an entity with no configured queries) is not a success or a failure, and a
monitor with nothing to do never raises.
"""

from dataclasses import dataclass

from errors import MonitorError


@dataclass
class UnitTally:
    """Mutable per-run counter of attempted source units for one monitor."""

    monitor: str
    succeeded: int = 0
    failed: int = 0

    @property
    def attempted(self) -> int:
        return self.succeeded + self.failed

    def record_success(self) -> None:
        """One unit produced a usable observation."""
        self.succeeded += 1

    def record_failure(self) -> None:
        """One unit failed, or returned nothing usable (e.g. a bot-challenge
        interstitial). Either way this run learned nothing from it."""
        self.failed += 1

    def raise_if_total_failure(self) -> None:
        """Raise ``MonitorError`` iff units were attempted and ALL of them
        failed. A no-op when nothing was attempted, or when at least one unit
        succeeded (a partial outage is still a successful run)."""
        if self.attempted > 0 and self.succeeded == 0:
            raise MonitorError(
                f"{self.monitor}: all {self.attempted} source unit(s) failed "
                f"this run; treating the run as unsuccessful so last_run is "
                f"not advanced and the monitor retries on the next pass"
            )
