"""Guards `data/overture-import-report.md` against drifting from the
committed data it describes (D-45): the report's headline numbers are
recomputed independently from the committed CSVs and the committed
per-decision file, never trusted as their own source of truth. A
hand-edited CSV or a regenerated-but-uncommitted report would otherwise
drift silently -- the same reason `routing.serializers.station_data_note()`
is derived-never-restated (see its own docstring), applied one layer out
here: the report is a RENDERING of the data, and this class is what makes
that rendering provably honest rather than merely plausible.
"""
import csv
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from routing.services import regions

DATA_DIR = Path(settings.BASE_DIR) / "data"
REPORT_PATH = DATA_DIR / "overture-import-report.md"
DECISIONS_PATH = DATA_DIR / "overture-dedupe-decisions.csv"
OVERTURE_STATIONS_PATH = DATA_DIR / "overture_stations.csv"
STATIONS_GEOCODED_PATH = DATA_DIR / "stations_geocoded.csv"


def _read_csv_rows(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


class OvertureReportDriftGuardTests(SimpleTestCase):
    """D-45: the committed report's headline counts must equal the
    committed CSVs' actual row counts by tier and source, so a hand-edited
    CSV or a regenerated-but-uncommitted report fails loudly rather than
    silently drifting. Four independent headline numbers are checked below,
    each against a value recomputed straight from the committed files, not
    against each other."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.report_text = REPORT_PATH.read_text(encoding="utf-8")
        cls.overture_rows = _read_csv_rows(OVERTURE_STATIONS_PATH)
        cls.opis_rows = _read_csv_rows(STATIONS_GEOCODED_PATH)
        cls.decision_rows = _read_csv_rows(DECISIONS_PATH)

    def test_kept_count_equals_overture_csv_row_count(self):
        """Headline number 1: the report's `Kept:` figure equals the
        committed Overture station CSV's actual data-row count."""
        match = re.search(r"- Kept: (\d+)", self.report_text)
        self.assertIsNotNone(match, "report has no 'Kept:' line")
        self.assertEqual(int(match.group(1)), len(self.overture_rows))

    def test_per_tier_drop_counts_equal_decisions_csv_counts(self):
        """Headline number 2: the report's tight-tier/city-tier match
        counts equal the counts derived from the committed per-decision
        CSV, not from the report's own dedup narrative."""
        tight_from_decisions = sum(
            1
            for row in self.decision_rows
            if row["decision"] == "dropped" and row["tier"] == "tight"
        )
        city_from_decisions = sum(
            1
            for row in self.decision_rows
            if row["decision"] == "dropped" and row["tier"] == "city"
        )

        tight_match = re.search(
            r"Tight-tier matches \(rooftop-precision existing rows\): (\d+)",
            self.report_text,
        )
        city_match = re.search(
            r"City-tier matches \(city-centroid existing rows, "
            r"brand\+city\+state\): (\d+)",
            self.report_text,
        )
        self.assertIsNotNone(tight_match, "report has no tight-tier line")
        self.assertIsNotNone(city_match, "report has no city-tier line")
        self.assertEqual(int(tight_match.group(1)), tight_from_decisions)
        self.assertEqual(int(city_match.group(1)), city_from_decisions)

    def test_no_match_count_equals_decisions_csv_kept_count(self):
        """The dedup stage's own kept/dropped split is internally
        consistent with the per-decision CSV -- a companion check to the
        tight/city counts above, over the same file's `kept` rows."""
        kept_from_decisions = sum(
            1 for row in self.decision_rows if row["decision"] == "kept"
        )
        match = re.search(r"No match \(kept as new\): (\d+)", self.report_text)
        self.assertIsNotNone(match, "report has no 'No match' line")
        self.assertEqual(int(match.group(1)), kept_from_decisions)

    def test_per_region_priced_row_breakdown_equals_overture_csv_grouped_by_region(
        self,
    ):
        """Headline number 3: the report's per-region priced-row breakdown
        equals the committed Overture CSV's own rows grouped by
        `regions.region_for_state`, region by region -- not merely equal in
        total."""
        recomputed = {}
        for row in self.overture_rows:
            region = regions.region_for_state(row["state"])
            recomputed[region] = recomputed.get(region, 0) + 1
        self.assertNotIn(
            None, recomputed, "an Overture row's state did not resolve to a region"
        )

        section_match = re.search(
            r"Priced rows by region:\n((?:  - .+\n)+)", self.report_text
        )
        self.assertIsNotNone(section_match, "report has no per-region breakdown")
        printed = {}
        for line in section_match.group(1).splitlines():
            region_name, count = line.strip().lstrip("- ").split(":")
            printed[region_name.strip()] = int(count.strip())

        self.assertEqual(printed, recomputed)

    def test_total_station_count_across_both_committed_csvs_has_no_id_collisions(
        self,
    ):
        """Headline number 4: the 'total station count across both CSVs'
        check -- not a single printed report line, but a genuine drift
        guard on the committed pair together. If either committed CSV ever
        gained a duplicate `opis_id`, or the two files' id spaces ever
        overlapped, the deduplicated union's size would fall short of the
        arithmetic sum -- exactly the failure mode a naive per-file row
        count would miss, and the identical property D-22's disjointness
        check proves from the identifier side rather than the report
        side."""
        overture_ids = [int(row["opis_id"]) for row in self.overture_rows]
        opis_ids = [int(row["opis_id"]) for row in self.opis_rows]

        self.assertEqual(
            len(overture_ids), len(set(overture_ids)), "duplicate Overture opis_id"
        )
        self.assertEqual(
            len(opis_ids), len(set(opis_ids)), "duplicate OPIS opis_id"
        )

        combined = set(overture_ids) | set(opis_ids)
        self.assertEqual(
            len(combined),
            len(overture_ids) + len(opis_ids),
            "opis_id space overlaps between the two committed CSVs",
        )
