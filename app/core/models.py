from dataclasses import dataclass


@dataclass
class Arrival:
    stop_id: str
    trip_id: str
    route: str
    due_min: int
    expected: int
    scheduled: int | None
    delay: int | None
    realtime: bool
    cancelled: bool = False  # server flag: cancelled trip or skipped stop; scheduled time only
