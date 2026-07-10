"""One-time, offline dev tool (not a shipped management command).

Downloads the Census Gazetteer national Places file, trims it to
(name, state, lat, lng) for lower-48 states only, and writes the
result to data/gazetteer_places_trimmed.csv.

The committed OUTPUT of this script is the deliverable (D-14) -- the
script itself is not run at container start or by any Django command.

Usage:
    .venv/Scripts/python.exe scripts/trim_gazetteer.py
"""
import csv
import io
import sys
import zipfile
from pathlib import Path

import requests

GAZETTEER_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    "2025_Gazetteer/2025_Gaz_place_national.zip"
)

# The dataset is lower-48 only (per CONTEXT.md profiling of the source CSV):
# drop Alaska, Hawaii, DC, and territories from the committed lookup to keep
# the join file lean.
LOWER_48_STATES = {
    "AL", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
    "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
}

OUTPUT_PATH = Path(__file__).resolve().parent.parent / "data" / "gazetteer_places_trimmed.csv"


def download_gazetteer_zip(url: str) -> bytes:
    """Download the Gazetteer national Places zip, failing loudly on a bad status
    (Pitfall D -- do not silently 404 on a hardcoded, year-specific URL)."""
    response = requests.get(url, timeout=120)
    if response.status_code != 200:
        raise RuntimeError(
            f"Failed to download Gazetteer file from {url}: "
            f"HTTP {response.status_code}"
        )
    return response.content


def extract_places_txt(zip_bytes: bytes) -> str:
    """Extract the single .txt member from the downloaded zip and decode it.

    The source file was probed directly (see 01-03-SUMMARY.md) and confirmed
    to be valid UTF-8 -- diacritic-bearing place names (e.g. Utqiagvik,
    La Canada Flintridge) decode correctly as UTF-8. Read as utf-8, not
    latin-1 (Assumptions Log A2).
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        txt_names = [name for name in archive.namelist() if name.endswith(".txt")]
        if not txt_names:
            raise RuntimeError("No .txt member found in Gazetteer zip archive")
        raw = archive.read(txt_names[0])
    return raw.decode("utf-8")


def trim_to_lower_48(text: str):
    """Parse the pipe-delimited Gazetteer text and yield trimmed rows for
    lower-48 states only."""
    reader = csv.DictReader(io.StringIO(text), delimiter="|")
    for row in reader:
        state = row["USPS"]
        if state not in LOWER_48_STATES:
            continue
        yield {
            "name": row["NAME"],
            "state": state,
            "lat": row["INTPTLAT"],
            "lng": row["INTPTLONG"],
        }


def write_trimmed_csv(rows, output_path: Path) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(output_path, "w", newline="", encoding="utf-8") as dst:
        writer = csv.DictWriter(dst, fieldnames=["name", "state", "lat", "lng"])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
            count += 1
    return count


def main() -> int:
    print(f"Downloading Gazetteer national Places file from {GAZETTEER_URL} ...")
    zip_bytes = download_gazetteer_zip(GAZETTEER_URL)
    print(f"Downloaded {len(zip_bytes)} bytes. Extracting ...")
    text = extract_places_txt(zip_bytes)
    print("Trimming to (name, state, lat, lng) for lower-48 states ...")
    rows = trim_to_lower_48(text)
    count = write_trimmed_csv(rows, OUTPUT_PATH)
    print(f"Wrote {count} rows to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
