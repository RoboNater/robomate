import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "measure_call_bytes", ROOT / "scripts/measure-call-bytes.py"
)
assert SPEC is not None and SPEC.loader is not None
MEASURE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MEASURE)


def test_the_published_sequence_loads_with_codex_duplicates_merged() -> None:
    calls = MEASURE.load_calls(MEASURE.DEFAULT_AUDIT)

    tools = {agent: [call["tool"] for call in sequence] for agent, sequence in calls.items()}
    assert tools["charlie"][:5] == [
        "check_in",
        "await_assignment",
        "get_role_guide",
        "report_progress",
        "submit_result",
    ]
    assert tools["charlie"].count("check_in") == 1
    assert tools["alice"].count("wait_for_event") == 21
    assert tools["alice"].count("check_merge_gate") == 3


def test_timeouts_are_read_from_recorded_durations() -> None:
    def wait(seconds: float, timeout_s: float = 120) -> dict[str, object]:
        return {
            "tool": "wait_for_event",
            "input": {"timeout_s": timeout_s},
            "timestamp": "2026-09-18T19:00:00.000Z",
            "completed_at": f"2026-09-18T19:00:{seconds:06.3f}Z",
        }

    assert MEASURE.timed_out(wait(1.02, timeout_s=1)) is True
    assert MEASURE.timed_out(wait(23.7)) is False
    assert MEASURE.timed_out({"tool": "wait_for_event", "input": {}}) is None


def test_work_blocks_are_the_calls_after_each_assignment() -> None:
    calls = [
        {"tool": name, "input": {}}
        for name in (
            "check_in",
            "await_assignment",
            "get_role_guide",
            "submit_result",
            "await_assignment",
            "await_assignment",
            "submit_result",
            "await_assignment",
        )
    ]

    check_in, blocks = MEASURE.work_blocks(calls)

    assert check_in["tool"] == "check_in"
    assert [[call["tool"] for call in block] for block in blocks] == [
        ["get_role_guide", "submit_result"],
        ["submit_result"],
    ]
