"""Resolve the local IANA time zone for a claim's own location.

Used so that pluvial date correction checks each claim's *local* calendar
day (correctly handling DST transitions and the different UTC offsets
across CONUS) instead of assuming a single national time zone.

Resolved once per claim, at the claim's own point — not per AORC pixel —
so that all 4 of a claim's bilinear-interpolation corner pixels (see
aorc_grid.latlon_to_bilinear_corners) share one, identical local-day-to-
UTC-hours conversion. That's what lets 04c combine the 4 corners' hourly
series with a plain elementwise weighted sum: their hours_utc arrays are
guaranteed to match by construction, rather than usually matching because
a pixel is small and DST/timezone-boundary edge cases are rare.
"""

import pandas as pd
from timezonefinder import TimezoneFinder

_tf = TimezoneFinder()


def point_timezones(lats, lons) -> pd.Series:
    """IANA time zone for each WGS84 point, e.g. 'America/Chicago'.

    Falls back to a fixed UTC offset derived from longitude (`Etc/GMT+-N`)
    for the rare point timezonefinder can't resolve (e.g. just offshore);
    fallback count is printed so it can be reviewed rather than silently
    mis-assigned to a neighboring zone.
    """
    tz_out, fallback_flags = [], []
    for lat, lon in zip(lats, lons):
        tz = _tf.timezone_at(lat=lat, lng=lon)
        used_fallback = tz is None
        if used_fallback:
            # Etc/GMT signs are inverted by POSIX convention (west is positive).
            offset_hours = round(-lon / 15)
            tz = f"Etc/GMT{'+' if offset_hours >= 0 else ''}{offset_hours}"
        tz_out.append(tz)
        fallback_flags.append(used_fallback)

    n_fallback = sum(fallback_flags)
    if n_fallback:
        print(f"  {n_fallback} / {len(tz_out):,} points used a fixed-offset timezone fallback")

    return pd.Series(tz_out)
