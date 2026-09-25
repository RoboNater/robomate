"""Consistent timestamps for persisted hub state."""

from datetime import UTC, datetime, timedelta


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime."""

    return datetime.now(UTC)


def to_iso(moment: datetime) -> str:
    """Render a datetime in the one format every persisted timestamp uses."""

    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def utcnow_iso() -> str:
    """Return a lexically sortable, timezone-aware UTC timestamp."""

    return to_iso(utcnow())


def iso_after(seconds: float) -> str:
    """Return the timestamp `seconds` from now, in the persisted format.

    Every stored timestamp is UTC with a fixed precision, so deadlines can be
    compared against them lexically in SQL rather than parsed row by row.
    """

    return to_iso(utcnow() + timedelta(seconds=seconds))
