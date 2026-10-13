"""Bounded retry of Windows sharing violations (#103, #114, #180)."""

from collections.abc import Callable

import pytest
from agent_hub_common.sharing import retry_sharing_violation


def _flaky(
    denials: int, error: type[OSError] = PermissionError
) -> tuple[list[int], Callable[[], str]]:
    calls: list[int] = []

    def operation() -> str:
        calls.append(len(calls))
        if len(calls) <= denials:
            raise error(13, "Permission denied")
        return "done"

    return calls, operation


def test_retries_once_per_delay_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr("agent_hub_common.sharing.time.sleep", slept.append)
    calls, operation = _flaky(2)
    assert retry_sharing_violation(operation, (0.1, 0.2)) == "done"
    assert (len(calls), slept) == (3, [0.1, 0.2])


def test_last_permission_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr("agent_hub_common.sharing.time.sleep", slept.append)
    calls, operation = _flaky(3)
    with pytest.raises(PermissionError):
        retry_sharing_violation(operation, (0.1, 0.2))
    assert (len(calls), slept) == (3, [0.1, 0.2])


def test_other_errors_and_no_delays_are_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_hub_common.sharing.time.sleep", pytest.fail)
    calls, operation = _flaky(1, FileNotFoundError)
    with pytest.raises(FileNotFoundError):
        retry_sharing_violation(operation, (0.1,))
    assert len(calls) == 1
    calls, operation = _flaky(1)
    with pytest.raises(PermissionError):
        retry_sharing_violation(operation, ())
    assert len(calls) == 1
