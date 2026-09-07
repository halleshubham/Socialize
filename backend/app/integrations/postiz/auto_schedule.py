"""Slot-building for the board's "Auto-schedule" bulk action - spreads a
brand's queued Approved posts across a date range at a configurable
posts-per-day frequency. Each resulting slot is handed to
postiz.send.send_content_item exactly like a single manual "Send to
Postiz" click, just with schedule_at set from the slot instead of a
per-item date picker (see routes_board.py::auto_schedule).
"""

from datetime import date, datetime, timedelta, timezone


def build_schedule_slots(
    start_date: date,
    end_date: date,
    posts_per_day: int,
    day_start_hour: float,
    day_end_hour: float,
    tz_offset_minutes: int,
) -> list[datetime]:
    """One UTC datetime per slot, day by day, earliest first. Slots within a
    day are spread evenly across [day_start_hour, day_end_hour) local time.
    tz_offset_minutes is the browser's Date.getTimezoneOffset() value at
    submit time (minutes to ADD to a local wall-clock time to get UTC, same
    convention JS uses) - applied uniformly across the whole range, so a DST
    boundary inside a long range is a known, minor v1 simplification."""
    if posts_per_day < 1 or end_date < start_date:
        return []
    span_hours = max(day_end_hour - day_start_hour, 0)
    slots: list[datetime] = []
    day = start_date
    while day <= end_date:
        for i in range(posts_per_day):
            hour_fraction = day_start_hour + (span_hours * i / posts_per_day if posts_per_day > 1 else 0)
            hour = int(hour_fraction)
            minute = int(round((hour_fraction - hour) * 60)) % 60
            local_naive = datetime(day.year, day.month, day.day, hour, minute)
            utc_dt = (local_naive + timedelta(minutes=tz_offset_minutes)).replace(tzinfo=timezone.utc)
            slots.append(utc_dt)
        day += timedelta(days=1)
    return slots
