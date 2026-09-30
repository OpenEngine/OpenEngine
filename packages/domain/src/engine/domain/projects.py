"""Standing instructions and limits for autonomous WorkOrders."""

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Project:
    project_id: str
    name: str
    repository: str
    workflow: str
    instructions: str
    timezone: str = "UTC"
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)
    start_time: str = "09:00"
    end_time: str = "17:00"
    daily_budget: int = 1
    enabled: bool = False
    requester: str | None = None
    budget_date: str = ""
    used_budget: int = 0
    last_run_id: str = ""
    error: str = ""

    def __post_init__(self) -> None:
        for key in ("name", "repository", "workflow", "instructions"):
            value = getattr(self, key)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} is required")
        ZoneInfo(self.timezone)
        for value in (self.start_time, self.end_time):
            if not isinstance(value, str) or len(value) != 5:
                raise ValueError("working hours must use HH:MM")
            time.fromisoformat(value)
        if self.start_time == self.end_time:
            raise ValueError("working hours must have different start and end times")
        if not self.weekdays or any(type(day) is not int or day not in range(7) for day in self.weekdays):
            raise ValueError("select at least one weekday (Monday=0 through Sunday=6)")
        if type(self.daily_budget) is not int or not 1 <= self.daily_budget <= 100:
            raise ValueError("daily budget must be between 1 and 100 WorkOrders")
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be a boolean")

    def local_date(self, now: datetime) -> str:
        return now.astimezone(ZoneInfo(self.timezone)).date().isoformat()

    def eligible(self, now: datetime) -> bool:
        local = now.astimezone(ZoneInfo(self.timezone))
        current = local.time().replace(tzinfo=None)
        start, end = time.fromisoformat(self.start_time), time.fromisoformat(self.end_time)
        day = local.weekday()
        if start < end:
            working = day in self.weekdays and start <= current < end
        else:
            working = (day in self.weekdays and current >= start) or (
                (local - timedelta(days=1)).weekday() in self.weekdays and current < end
            )
        used = self.used_budget if self.budget_date == self.local_date(now) else 0
        return self.enabled and working and used < self.daily_budget
