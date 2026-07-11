"""US Census Bulk Geocoder `addressbatch` HTTP client (geocoding pass 1).

Chunked, resumable multipart POST wrapper plus a defensive response parser
that branches on field count rather than assuming a fixed column shape:
`No_Match` rows carry 3 fields, `Match` rows carry 8, and the
coordinate field is a single quoted "longitude,latitude" string.

Given this dataset's highway-exit "addresses", this pass resolves only a
handful of rows -- the Gazetteer pass (gazetteer.py) delivers the bulk.
Network failure here is non-fatal: `submit_chunk` may raise on a transport
error, and the caller (geocode_stations) catches it per-chunk, logs it,
and leaves those rows `pending` for a later resumed run.

Pure module: no Django import, no DB access.
"""
import csv
import io

import requests

# The /locations/ endpoint (NOT /geographies/, which additionally requires
# a `vintage` param and returns extra tract/county columns this project
# doesn't need).
ADDRESSBATCH_URL = "https://geocoding.geo.census.gov/geocoder/locations/addressbatch"

# "Always current" address-range locator benchmark -- documented alias so
# callers don't need to track a moving numeric/year-suffixed benchmark name.
BENCHMARK = "Public_AR_Current"

# Within the recommended "few-hundred-to-1,000" range for addressbatch
# chunk size.
CHUNK_SIZE = 500


def build_chunk_csv(rows) -> bytes:
    """Build a NO-HEADER CSV of (unique_id, street_address, city, state, zip)
    rows for submission as the addressbatch `addressFile`.

    `rows`: iterable of 5-tuples in that fixed column order (the Census
    addressbatch format has no header row).
    """
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def submit_chunk(rows):
    """POST one chunk of rows to the addressbatch endpoint and parse the
    response. May raise on a transport error (connection failure, timeout,
    non-2xx status) -- the caller is responsible for catching this per
    chunk, logging it, and leaving those rows `pending` for a later
    resumed run; this function does not swallow such errors itself.
    """
    csv_bytes = build_chunk_csv(rows)
    response = requests.post(
        ADDRESSBATCH_URL,
        files={"addressFile": ("chunk.csv", csv_bytes, "text/csv")},
        data={"benchmark": BENCHMARK},
        timeout=120,
    )
    response.raise_for_status()
    return parse_addressbatch_response(response.text)


def parse_addressbatch_response(text: str):
    """Parse the addressbatch response CSV, branching on field count rather
    than assuming a fixed shape. `No_Match` rows have 3 fields;
    `Match` rows have 8, with the coordinate field a single quoted
    "longitude,latitude" string (split variables are named
    explicitly `longitude, latitude`, never generic).

    An unrecognized/short row shape (e.g. an undocumented `Tie` shape, see
    Assumptions Log A1) falls through to the base 3-field handling and is
    treated as unmatched rather than crashing.
    """
    records = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 3:
            # Defensively skip a genuinely malformed/empty row rather than
            # raising -- one bad response row should never abort the chunk.
            continue

        record = {
            "id": row[0],
            "input_address": row[1],
            "match_status": row[2],
        }

        if record["match_status"] == "Match" and len(row) >= 8:
            longitude, latitude = row[5].split(",")
            record.update(
                {
                    "match_type": row[3],
                    "matched_address": row[4],
                    "longitude": float(longitude),
                    "latitude": float(latitude),
                    "tigerlineid": row[6],
                    "side": row[7],
                }
            )
        # No_Match (3 fields) and any unrecognized/short shape (e.g. Tie)
        # fall through here with no coordinates -- treated as unmatched.

        records.append(record)
    return records
