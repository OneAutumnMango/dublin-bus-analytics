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
    vehicle_id: str | None
    possibly_passed: bool
