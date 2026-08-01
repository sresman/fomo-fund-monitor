from __future__ import annotations

"""Tests for errors.py."""

from errors import AlertError, ConfigError, MonitorError, StateError


def test_config_and_state_errors_distinct() -> None:
    assert issubclass(ConfigError, Exception)
    assert issubclass(StateError, Exception)
    # Distinct types (compared by name to avoid a statically-known identity
    # check that mypy flags as non-overlapping).
    assert ConfigError.__name__ != StateError.__name__
    assert not issubclass(ConfigError, StateError)
    assert not issubclass(StateError, ConfigError)


def test_monitor_error_distinct() -> None:
    assert issubclass(MonitorError, Exception)
    # A distinct direct subclass of Exception, inheriting none of its siblings.
    assert not issubclass(MonitorError, ConfigError)
    assert not issubclass(MonitorError, StateError)
    assert not issubclass(MonitorError, AlertError)
    assert not issubclass(ConfigError, MonitorError)
    assert not issubclass(StateError, MonitorError)
    assert not issubclass(AlertError, MonitorError)
