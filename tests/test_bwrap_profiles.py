"""Conservative detection of AppArmor bwrap attachment conflicts."""

import importlib.util
from pathlib import Path

import pytest

PATH = (
    Path(__file__).resolve().parents[1] / "scripts/host-setup/ubuntu-24.04/check-bwrap-profiles.py"
)
SPEC = importlib.util.spec_from_file_location("bwrap_profiles", PATH)
assert SPEC and SPEC.loader
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)


@pytest.mark.parametrize(
    "header",
    [
        "profile /usr/bin/bwrap {",
        "profile other /{usr/,}bin/bwrap flags=(unconfined) {",
        "profile other @{bindir}/bwrap {",
        "/usr/bin/bwrap {",
        "profile other /usr/bin/* {",
    ],
)
def test_conflicting_attachment_is_detected(header: str) -> None:
    assert any(CHECK.may_match(p) for p in CHECK.attachments(header))


def test_unrelated_profile_does_not_conflict() -> None:
    assert not any(CHECK.may_match(p) for p in CHECK.attachments("profile x /usr/bin/cat {"))
