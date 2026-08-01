from __future__ import annotations

"""Shared exception types for fomo-fund-monitor.

Leaf module: imports nothing from the app so ``config.py`` and
``state_manager.py`` can both import from it without creating an import cycle.

``ConfigError`` and ``StateError`` are two DISTINCT direct subclasses of
``Exception``. Neither inherits the other, so callers can catch them
independently.
"""


class ConfigError(Exception):
    """Raised when ``config.yaml`` is missing, unreadable, malformed, or fails
    schema/semantic validation."""


class StateError(Exception):
    """Raised when an on-disk state file is present but corrupt (invalid JSON,
    wrong container/element shape, or an unparseable/naive/non-string
    timestamp)."""


class AlertError(Exception):
    """Raised when an alerting operation fails: missing/blank credential or
    recipient env vars, an email/SMS send failure, or a formatting failure.

    A distinct direct subclass of ``Exception`` (parallel to ``ConfigError`` /
    ``StateError``); it inherits neither, so callers can catch it independently.
    Sender messages are sanitized and never contain credential values."""


class MonitorError(Exception):
    """Raised when a monitor's data source is unreachable, times out, or returns
    malformed/unparseable data.

    A distinct direct subclass of ``Exception`` (parallel to ``ConfigError`` /
    ``StateError`` / ``AlertError``); it inherits none of them, so callers can
    catch it independently."""


class DispatchBridgeError(Exception):
    """Raised internally by the concrete ``repository_dispatch`` bridge on a
    transport/HTTP fault (timeout, connection error, retry exhausted, or a
    permanent non-2xx response).

    The orchestrator ALWAYS wraps ``bridge.fire(...)`` in ``try/except`` so this
    NEVER escapes a run -- a bridge failure is logged as a WARNING and never
    affects alerting, mark-seen, ``record_run``, or the exit code. Its message is
    sanitized and NEVER contains the PAT."""


class DispatchBridgeAuthError(DispatchBridgeError):
    """Raised specifically when the bridge POST is rejected with a 401 or 403.

    A subclass of ``DispatchBridgeError`` so the orchestrator can detect an
    auth/scope failure distinctly and short-circuit all remaining fires for the
    run (a bad/expired/insufficient-scope PAT must not produce N doomed POSTs
    and N identical WARNINGs)."""
