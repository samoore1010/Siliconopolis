"""When each agent runs next (mirrors the Railway cron schedules in .railway/railway.ts, all UTC)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

# (weekday Mon=0 or None for daily/hourly, hour UTC or None for hourly, minute)
SCHEDULES = {
    "researcher": (0, 13, 0),   # Monday 6:00 AM Phoenix
    "daily": (None, 14, 0),     # Creator -> QA -> Lister, 7:00 AM Phoenix
    "analyst": (0, 3, 0),       # Sunday 8:00 PM Phoenix
    "fulfiller": (None, None, 0),  # hourly
    "editor": (None, None, 30),    # hourly at :30, so polishes never wait behind the daily build
}
AGENT_SCHEDULE = {"researcher": "researcher", "creator": "daily", "qa": "daily", "lister": "daily",
                  "analyst": "analyst", "fulfiller": "fulfiller", "editor": "editor"}


def next_run(agent: str, now: datetime | None = None) -> datetime:
    now = now or datetime.now(UTC)
    weekday, hour, minute = SCHEDULES[AGENT_SCHEDULE.get(agent, agent)]
    if hour is None:  # hourly
        nxt = now.replace(minute=minute, second=0, microsecond=0)
        return nxt if nxt > now else nxt + timedelta(hours=1)
    nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if weekday is None:
        return nxt if nxt > now else nxt + timedelta(days=1)
    nxt += timedelta(days=(weekday - now.weekday()) % 7)
    return nxt if nxt > now else nxt + timedelta(days=7)
