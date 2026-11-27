"""Parse server Retry-After instructions without shortening the requested wait."""
from datetime import datetime,timezone
from email.utils import parsedate_to_datetime
import math


def retry_after_seconds(value):
    if value is None:
        return None
    try:
        seconds=float(value)
    except (ValueError,TypeError):
        try:
            date=parsedate_to_datetime(value)
            if date.tzinfo is None:
                date=date.replace(tzinfo=timezone.utc)
            seconds=(date-datetime.now(timezone.utc)).total_seconds()
        except (ValueError,TypeError,OverflowError):
            return None
    return max(0,seconds) if math.isfinite(seconds) else None
