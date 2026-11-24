"""Comparator corpus: OD pairs, weekday time slots and the schedule check run before any call."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time

from delhi_transit.comparator.client import validate_departure_time
from delhi_transit.core.config import IST

TARGET_WEEKDAY_TUESDAY = 1  # Matches held-out Tuesday 2026-10-27 and brief example Tuesday 08:30


@dataclass(frozen=True)
class ODPair:
    od_id: str
    group: str
    label: str
    from_name: str
    from_lat: float
    from_lng: float
    to_name: str
    to_lat: float
    to_lng: float


@dataclass(frozen=True)
class TimeSlot:
    slot_id: str
    label: str
    hour: int
    minute: int


DEFAULT_TIME_SLOTS: tuple[TimeSlot, ...] = (
    TimeSlot("morning_peak", "08:30 IST", 8, 30),
    TimeSlot("midday", "14:00 IST", 14, 0),
    TimeSlot("evening_peak", "18:30 IST", 18, 30),
)

BRIEF, UP_717A, DOWN_717A, CITY = "brief_example", "corridor_717a_up", "corridor_717a_down", "city_wide"

# od_id, group, label, then origin and destination as (name, lat, lng).
_OD_ROWS = (
    # Brief example
    ("od_01", BRIEF, "Kashmere Gate ISBT -> Nehru Place",
     "Kashmere Gate ISBT", 28.66750, 77.22820, "Nehru Place", 28.54940, 77.25100),
    # 717A UP corridor (route 2171: Badarpur Border -> Kapashera Border)
    ("od_02", UP_717A, "Badarpur Border -> Kapashera Border",
     "Badarpur Border (T)", 28.49313, 77.30373, "Kapashera Border", 28.52143, 77.08203),
    ("od_03", UP_717A, "Badarpur Border -> Lado Sarai Crossing",
     "Badarpur Border (T)", 28.49313, 77.30373, "Lado Sarai Crossing", 28.52283, 77.19190),
    ("od_04", UP_717A, "Air Force Station -> Vasant Kunj Sec A",
     "Air Force Station", 28.51140, 77.25648, "Vasant Kunj Sec A Pkt B/C", 28.51034, 77.16976),
    ("od_05", UP_717A, "Ambedkar Nagar Depot -> Masood Pur Village",
     "Ambedkar Nagar Depot", 28.51518, 77.22819, "Masood Pur Village", 28.52548, 77.15433),
    ("od_06", UP_717A, "Maidan Garhi Crossing -> Rajokari Xing",
     "Maidan Garhi Crossing (SDM Court)", 28.51907, 77.20880, "Rajokari Xing", 28.53492, 77.11141),
    # 717A DOWN corridor (route 2170: Kapashera Border -> Badarpur Border)
    ("od_07", DOWN_717A, "Kapashera Border -> Badarpur Border",
     "Kapashera Border", 28.52143, 77.08203, "Badarpur Border (T)", 28.49313, 77.30373),
    ("od_08", DOWN_717A, "Kapashera Border -> Maidan Garhi Crossing",
     "Kapashera Border", 28.52143, 77.08203, "Maidan Garhi Crossing (SDM Court)", 28.51947, 77.20777),
    ("od_09", DOWN_717A, "Shiv Murti NH8 -> Ambedkar Nagar Depot",
     "Shiv Murti 2 (NH8)", 28.53520, 77.10868, "Ambedkar Nagar Depot", 28.51527, 77.22840),
    ("od_10", DOWN_717A, "Vasant Kunj Sec-A -> Air Force Station",
     "Vasant Kunj Sec-A / LBS", 28.51165, 77.16678, "Air Force Station", 28.51168, 77.25547),
    ("od_11", DOWN_717A, "Ahinsa Sthal -> Lal Kuan",
     "Ahinsa Sthal", 28.52044, 77.18940, "Lal Kuan", 28.50252, 77.28455),
    # Diverse Delhi city-wide bus corridors
    ("od_12", CITY, "Azadpur Terminal -> Kashmere Gate ISBT",
     "Azadpur Terminal", 28.70768, 77.17816, "Kashmere Gate ISBT", 28.66750, 77.22820),
    ("od_13", CITY, "Kashmere Gate ISBT -> AIIMS / Safdarjung",
     "Kashmere Gate ISBT", 28.66750, 77.22820, "Safdarjung Hospital / AIIMS", 28.56725, 77.20773),
    ("od_14", CITY, "Shivaji Stadium -> Nehru Place",
     "Shivaji Stadium Terminal", 28.63306, 77.21546, "Nehru Place", 28.54940, 77.25100),
    ("od_15", CITY, "Anand Vihar ISBT -> ITO",
     "Anand Vihar ISBT Main Road", 28.64701, 77.31796, "ITO BSZ Marg", 28.62909, 77.24142),
    ("od_16", CITY, "Anand Vihar ISBT -> Kashmere Gate ISBT",
     "Anand Vihar ISBT Main Road", 28.64701, 77.31796, "Kashmere Gate ISBT", 28.66750, 77.22820),
    ("od_17", CITY, "Sarai Kale Khan ISBT -> Dhaula Kuan",
     "ISBT Sarai Kale Khan", 28.59087, 77.25817, "ARSD College (Dhaula Kuan)", 28.59077, 77.16838),
    ("od_18", CITY, "Dhaula Kuan -> AIIMS / Safdarjung",
     "ARSD College (Dhaula Kuan)", 28.59077, 77.16838, "Safdarjung Hospital / AIIMS", 28.56725, 77.20773),
    ("od_19", CITY, "Karol Bagh Terminal -> Lajpat Nagar",
     "Karol Bagh Terminal", 28.64837, 77.20066, "Lajpat Nagar Crossing", 28.56418, 77.25037),
    ("od_20", CITY, "Uttam Nagar Terminal -> Shivaji Stadium",
     "Uttam Nagar Terminal", 28.62547, 77.06628, "Shivaji Stadium Terminal", 28.63306, 77.21546),
    ("od_21", CITY, "Uttam Nagar Terminal -> Dhaula Kuan",
     "Uttam Nagar Terminal", 28.62547, 77.06628, "ARSD College (Dhaula Kuan)", 28.59077, 77.16838),
    ("od_22", CITY, "New Delhi Railway Station -> Hauz Khas",
     "New Delhi Railway Station Gate 2", 28.64177, 77.22359, "Hauz Khas", 28.55300, 77.20490),
    ("od_23", CITY, "Mehrauli Terminal -> AIIMS / Safdarjung",
     "Mehrauli Terminal", 28.52132, 77.18305, "Safdarjung Hospital / AIIMS", 28.56725, 77.20773),
    ("od_24", CITY, "Punjabi Bagh Terminal -> Karol Bagh Terminal",
     "Punjabi Bagh Terminal", 28.67548, 77.14046, "Karol Bagh Terminal", 28.64837, 77.20066),
    ("od_25", CITY, "Shahdara -> Red Fort",
     "Shahdara (Shyam Lal College)", 28.67367, 77.28359, "Red Fort", 28.65801, 77.23696),
    ("od_26", CITY, "Lajpat Nagar -> Badarpur Border",
     "Lajpat Nagar Crossing", 28.56418, 77.25037, "Badarpur Border (T)", 28.49313, 77.30373),
    ("od_27", CITY, "ITO -> Nehru Place",
     "ITO BSZ Marg", 28.62909, 77.24142, "Nehru Place", 28.54940, 77.25100),
    ("od_28", CITY, "Azadpur Terminal -> Punjabi Bagh Terminal",
     "Azadpur Terminal", 28.70768, 77.17816, "Punjabi Bagh Terminal", 28.67548, 77.14046),
    ("od_29", CITY, "Hauz Khas -> Kalkaji Mandir",
     "Hauz Khas", 28.55300, 77.20490, "Kalkaji Mandir", 28.55219, 77.26450),
    ("od_30", CITY, "Nehru Place -> Sarai Kale Khan ISBT",
     "Nehru Place", 28.54940, 77.25100, "ISBT Sarai Kale Khan", 28.59087, 77.25817),
)
DEFAULT_OD_PAIRS: tuple[ODPair, ...] = tuple(ODPair(*row) for row in _OD_ROWS)


def validate_comparison_schedule(
    target_date: date,
    time_slots: tuple[TimeSlot, ...],
    *,
    now: datetime | None,
    cache_only: bool,
) -> list[tuple[TimeSlot, datetime]]:
    """Validate the declared Tuesday and every canonical slot before any client call."""
    if target_date.weekday() != TARGET_WEEKDAY_TUESDAY:
        raise ValueError(
            f"Google comparator date must be a Tuesday matching the held-out weekday; "
            f"got {target_date.isoformat()} ({target_date.strftime('%A')})"
        )
    canonical_slots = {slot.slot_id: slot for slot in DEFAULT_TIME_SLOTS}
    slot_datetimes: list[tuple[TimeSlot, datetime]] = []
    for slot in time_slots:
        canonical = canonical_slots.get(slot.slot_id)
        if canonical != slot:
            raise ValueError(f"Comparator slot {slot.slot_id!r} does not match its declared weekday time")
        depart = datetime.combine(target_date, time(slot.hour, slot.minute), tzinfo=IST)
        validate_departure_time(depart, now=now, allow_past_for_cache=cache_only)
        slot_datetimes.append((slot, depart))
    return slot_datetimes
