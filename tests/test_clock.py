from datetime import datetime

from agent_hub_common import iso_after, to_iso, utcnow, utcnow_iso


def test_utcnow_iso_is_timezone_aware_and_sortable() -> None:
    first = utcnow_iso()
    second = utcnow_iso()

    assert first.endswith("Z")
    offset = datetime.fromisoformat(first).utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 0
    assert first <= second


def test_relative_timestamps_share_the_stored_format() -> None:
    now = utcnow_iso()
    later = iso_after(60)
    earlier = iso_after(-60)

    # Leases are compared lexically in SQL, so ordering must survive as text.
    assert earlier < now < later
    assert later.endswith("Z")
    assert to_iso(utcnow()) >= now
