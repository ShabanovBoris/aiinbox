"""Shared IANA local-calendar to UTC interval conversion for durable queries."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def local_dates_utc_window(
    first_date: date, end_date_exclusive: date, zone: ZoneInfo
) -> tuple[datetime, datetime]:
    """Return a half-open naive-UTC interval for local dates, preserving DST.

    Notification budgets and motivation facts must agree on local midnight;
    converting each endpoint separately keeps 23/25-hour days calendar-correct.
    """
    if end_date_exclusive <= first_date:
        raise ValueError("end_date_exclusive must be later than first_date")
    start = datetime.combine(first_date, time.min, tzinfo=zone).astimezone(UTC)
    end = datetime.combine(end_date_exclusive, time.min, tzinfo=zone).astimezone(UTC)
    return start.replace(tzinfo=None), end.replace(tzinfo=None)


def local_day_window(now: datetime, zone: ZoneInfo) -> tuple[date, datetime, datetime]:
    """Project one UTC instant to its local date and DST-safe UTC day bounds."""
    instant = now.astimezone(UTC) if now.tzinfo is not None else now.replace(tzinfo=UTC)
    local_date = instant.astimezone(zone).date()
    start, end = local_dates_utc_window(local_date, local_date + timedelta(days=1), zone)
    return local_date, start, end
