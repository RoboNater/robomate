import os
import sys
import threading
from pathlib import Path

import agent_hub_common.token as token_module
import pytest
from agent_hub_common import TokenError, load_or_create_token, token_matches


def test_token_file_is_created_once_with_private_permissions(tmp_path: Path) -> None:
    path = tmp_path / "secrets" / "token"

    first = load_or_create_token(None, path)
    second = load_or_create_token(None, path)

    assert first == second
    assert len(first) >= 32
    if sys.platform != "win32":
        assert os.stat(path).st_mode & 0o777 == 0o600
    assert token_matches(first, second)
    assert not token_matches(first, "wrong")


def test_explicit_token_does_not_touch_disk(tmp_path: Path) -> None:
    path = tmp_path / "token"

    assert load_or_create_token("injected-secret", path) == "injected-secret"
    assert not path.exists()


@pytest.mark.skipif(
    sys.platform == "win32", reason="Windows does not support POSIX permission bits"
)
def test_world_readable_token_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text("exposed-secret\n", encoding="utf-8")
    path.chmod(0o644)

    with pytest.raises(TokenError, match="group or others"):
        load_or_create_token(None, path)


def test_explicit_token_is_normalized(tmp_path: Path) -> None:
    assert load_or_create_token("  injected-secret  ", tmp_path / "unused") == "injected-secret"


def test_failed_token_write_removes_partial_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "token"

    def fail_sync(_descriptor: int) -> None:
        raise OSError("simulated write failure")

    monkeypatch.setattr(os, "fsync", fail_sync)

    with pytest.raises(TokenError, match="cannot write"):
        load_or_create_token(None, path)
    assert not path.exists()


def test_a_token_being_written_by_another_process_is_waited_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two `robomate up`s race to create the shared operator token (#128)."""

    path = tmp_path / "operator-token"
    path.touch(mode=0o600)
    writer = threading.Timer(0.1, lambda: path.write_text("written-late\n"))
    writer.start()
    try:
        assert load_or_create_token(None, path) == "written-late"
    finally:
        writer.join()

    abandoned = tmp_path / "abandoned"
    abandoned.touch(mode=0o600)
    monkeypatch.setattr(token_module, "CREATION_GRACE_S", 0.05)
    with pytest.raises(TokenError, match="empty"):
        load_or_create_token(None, abandoned)
