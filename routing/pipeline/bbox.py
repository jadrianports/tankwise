"""Continental-US coordinate bounding-box validator (D-05).

The single persistence gate for every geocoded coordinate, regardless of
which pass produced it (Census addressbatch or Gazetteer centroid join).
Its primary payoff is rejecting a transposed (lng, lat) coordinate pair --
the specific bug D-05 exists to catch (Pitfall B).

Pure module: no Django import, no DB access (D-23).
"""
from decimal import Decimal

# Approximate continental US (lower-48) bounds. Deliberately generous so a
# legitimate lower-48 coordinate is never rejected; the goal is catching
# gross errors (0,0; transposed axes; off-continent points), not tight
# geofencing.
LAT_MIN = Decimal("24.4")
LAT_MAX = Decimal("49.4")
LON_MIN = Decimal("-125.0")
LON_MAX = Decimal("-66.9")


def is_valid(lat, lng) -> bool:
    """Return True only when lat/lng fall within the continental-US bbox.

    Accepts Decimal, float, or int for both arguments.
    """
    lat_value = Decimal(str(lat))
    lng_value = Decimal(str(lng))
    return LAT_MIN <= lat_value <= LAT_MAX and LON_MIN <= lng_value <= LON_MAX
