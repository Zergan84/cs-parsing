"""Normalize explicit Couchsurfing activity evidence without inventing recency."""

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Dict, Optional


RECENT = "RECENTLY_ACTIVE"
ACTIVE = "ACTIVE"
NOT_RECENT = "NOT_RECENTLY_ACTIVE"
UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ActivityEvidence:
    """Canonical activity value carried from a source observation to persistence."""
    raw_value: Optional[str]
    status: str
    age_days: Optional[int]
    last_active_at: Optional[str]
    checked_at: Optional[str]
    source: Optional[str]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "raw": self.raw_value, "status": self.status, "days": self.age_days,
            "at": self.last_active_at, "checked_at": self.checked_at, "source": self.source,
        }


def activity_evidence(
    *, is_recently_active: Any = None, raw_value: Any = None,
    checked_at: Optional[str] = None, source: Optional[str] = None,
    now: Optional[datetime] = None,
) -> ActivityEvidence:
    parsed = parse_activity(is_recently_active=is_recently_active, raw_value=raw_value, now=now)
    return ActivityEvidence(
        raw_value=parsed["raw"], status=parsed["status"], age_days=parsed["days"],
        last_active_at=parsed["at"], checked_at=checked_at, source=source,
    )


def parse_activity(
    *,
    is_recently_active: Any = None,
    raw_value: Any = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Return normalized status, original evidence, age in days, and exact timestamp.

    Boolean API evidence is authoritative. Text is parsed conservatively; a missing
    field remains UNKNOWN and is never treated as recently active.
    """
    if isinstance(is_recently_active, bool) or str(is_recently_active).strip().lower() in {"true", "false", "1", "0", "yes", "no"}:
        explicit_active = is_recently_active is True or str(is_recently_active).strip().lower() in {"true", "1", "yes"}
        date_evidence = parse_activity(raw_value=raw_value, now=now) if raw_value is not None else {}
        return {
            "status": RECENT if explicit_active else NOT_RECENT,
            "raw": str(raw_value).strip() if raw_value is not None and str(raw_value).strip() else ("isRecentlyActive=true" if explicit_active else "isRecentlyActive=false"),
            "days": date_evidence.get("days"),
            "at": date_evidence.get("at"),
        }

    raw = str(raw_value).strip() if raw_value is not None else ""
    if not raw:
        return {"status": UNKNOWN, "raw": None, "days": None, "at": None}

    text = " ".join(raw.split())
    lower = text.casefold()
    if re.search(r"\b(not recently active|inactive|not active recently)\b|не активен|давно не активен", lower):
        return {"status": NOT_RECENT, "raw": text, "days": None, "at": None}
    if re.search(r"\b(online now|active now|currently active)\b|сейчас в сети", lower):
        return {"status": ACTIVE, "raw": text, "days": 0, "at": None}
    if re.search(r"\b(recently active|active recently)\b|активен недавно|недавно актив", lower):
        return {"status": RECENT, "raw": text, "days": None, "at": None}

    age = re.search(r"\b(?:last\s+active\s+|active\s+)?(\d+)\s*(minutes?|hours?|days?|weeks?|months?|years?)\s+ago\b", lower)
    if age:
        amount = int(age.group(1))
        unit = age.group(2)
        factors = {"minute": 1 / 1440, "hour": 1 / 24, "day": 1, "week": 7, "month": 30, "year": 365}
        unit_key = next(key for key in factors if unit.startswith(key))
        days = max(0, round(amount * factors[unit_key]))
        return {"status": RECENT if days <= 30 else NOT_RECENT, "raw": text, "days": days, "at": None}

    # Only retain an exact date when the source gave one; do not synthesize a date
    # from a vague "recently" badge.
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        age_days = ((now or datetime.now()).date() - stamp.date()).days
        return {"status": RECENT if 0 <= age_days <= 30 else NOT_RECENT,
                "raw": text, "days": age_days,
                "at": stamp.isoformat()}
    except (ValueError, TypeError):
        pass
    if re.fullmatch(r"\d{10,13}", text):
        try:
            numeric = int(text)
            stamp = datetime.fromtimestamp(numeric / (1000 if len(text) == 13 else 1))
            age_days = ((now or datetime.now()).date() - stamp.date()).days
            return {"status": RECENT if 0 <= age_days <= 30 else NOT_RECENT,
                    "raw": text, "days": age_days, "at": stamp.isoformat()}
        except (OverflowError, OSError, ValueError):
            pass
    try:
        stamp_date = date.fromisoformat(text[:10])
        days = ((now or datetime.now()).date() - stamp_date).days
        return {"status": RECENT if 0 <= days <= 30 else NOT_RECENT, "raw": text, "days": days, "at": stamp_date.isoformat()}
    except (ValueError, TypeError):
        pass

    return {"status": UNKNOWN, "raw": text, "days": None, "at": None}
