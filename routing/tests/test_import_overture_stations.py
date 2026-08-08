"""Tests for the pure Overture transform (`routing.pipeline.overture`) and
its thin command (`import_overture_stations`).

`ImportOvertureStationsCommandTests` and `ApplyHygieneUnitTests` cover
hygiene, brand, price, identity, precision and provenance behaviour against
the hand-authored fixture extract, with dedup a deliberate no-op (empty
existing dataset) so none of that behaviour is disturbed by plan 22-11's
new stage. `OvertureDedupeReportTests` covers the dedup stage itself, end
to end through the command, against a small existing-dataset fixture
carrying one tight-tier and one city-tier match by design.
`OvertureIdCollisionRaiseTests` proves the collision guard's raise branch is
actually reachable via the pinned witness pair -- a natural collision is
unlikely at fixture (or even real import) scale, so only the witness pair
exercises that path. `OvertureTransformDeterminismTests` proves whole-file
and identifier determinism, disjointness against the real committed OPIS
dataset, and dry-run behaviour. D-22's third check (a pinned route solving
to a byte-identical plan across two independent imports) needs the real
dataset and the corridor/DP machinery and belongs to plan 22-12, not here.
"""
import csv
import io
import tempfile
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from routing.pipeline import overture, overture_dedupe, overture_scope
from routing.services import corridor, regions
from routing.services.solver import solve
from routing.tests.test_corridor_fixtures import (
    factor_lookup_for_basis,
    load_corridor_route,
)

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "overture"
FIXTURE_PATH = FIXTURE_DIR / "raw_extract_sample.csv"
EMPTY_EXISTING_PATH = FIXTURE_DIR / "existing_stations_empty.csv"
SAMPLE_EXISTING_PATH = FIXTURE_DIR / "existing_stations_sample.csv"
STATIONS_GEOCODED_PATH = Path(settings.BASE_DIR) / "data" / "stations_geocoded.csv"
RAW_EXTRACT_PATH = Path(settings.BASE_DIR) / "data" / "overture_raw_extract.csv"
OVERTURE_STATIONS_PATH = Path(settings.BASE_DIR) / "data" / "overture_stations.csv"


def _read_fixture_rows(existing_rows=()):
    with open(FIXTURE_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return overture.transform(reader, list(existing_rows))


class ParseExtractRowsHeaderTests(SimpleTestCase):
    def test_missing_required_column_raises_loudly(self):
        reader = csv.DictReader(io.StringIO("gers_id,name\n1,Test\n"))
        with self.assertRaises(overture.MalformedExtractHeaderError):
            overture.parse_extract_rows(reader)


class ApplyHygieneUnitTests(SimpleTestCase):
    """Direct unit coverage of `apply_hygiene`, independent of price
    assignment or identity minting -- the level the four-bucket invariant
    and the empty/closed operating-status behaviour are stated at."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with open(FIXTURE_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            cls.parsed_rows, cls.malformed_count = overture.parse_extract_rows(reader)
        cls.kept, cls.counts = overture.apply_hygiene(cls.parsed_rows)
        cls.kept_by_gers_id = {row.gers_id: row for row in cls.kept}

    def test_four_hygiene_bucket_counts_plus_kept_equal_parsed_row_count(self):
        total = sum(self.counts.values()) + len(self.kept)
        self.assertEqual(total, len(self.parsed_rows))

    def test_only_the_four_hygiene_buckets_are_present(self):
        self.assertEqual(
            set(self.counts.keys()),
            {"mojibake", "alt_fuel_only", "closed_status", "below_confidence_floor"},
        )

    def test_empty_operating_status_row_survives_apply_hygiene(self):
        # The row that catches the negation bug: a NULL/blank
        # operating_status cell means unknown, not closed, and must be
        # retained.
        self.assertIn("11111111-1111-4111-8111-111111111107", self.kept_by_gers_id)

    def test_permanently_closed_row_does_not_survive_apply_hygiene(self):
        self.assertNotIn("11111111-1111-4111-8111-111111111106", self.kept_by_gers_id)


class ImportOvertureStationsCommandTests(SimpleTestCase):
    """Covers `transform()` end to end -- hygiene, brand, price, identity,
    precision and provenance -- against the hand-authored fixture extract."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.station_rows, cls.report, cls.decisions = _read_fixture_rows()
        cls.by_gers_id = {row.gers_id: row for row in cls.station_rows}

    def test_mojibake_row_dropped_and_counted(self):
        self.assertNotIn("11111111-1111-4111-8111-111111111104", self.by_gers_id)
        self.assertEqual(self.report.bucket_counts["mojibake"], 1)

    def test_alt_fuel_row_dropped_and_counted(self):
        self.assertNotIn("11111111-1111-4111-8111-111111111105", self.by_gers_id)
        self.assertEqual(self.report.bucket_counts["alt_fuel_only"], 1)

    def test_closed_status_row_dropped_and_counted(self):
        self.assertNotIn("11111111-1111-4111-8111-111111111106", self.by_gers_id)
        self.assertEqual(self.report.bucket_counts["closed_status"], 1)

    def test_empty_operating_status_row_survives(self):
        self.assertIn("11111111-1111-4111-8111-111111111107", self.by_gers_id)

    def test_below_confidence_floor_row_dropped_and_counted(self):
        self.assertNotIn("11111111-1111-4111-8111-111111111108", self.by_gers_id)
        self.assertEqual(self.report.bucket_counts["below_confidence_floor"], 1)

    def test_blank_state_row_routed_to_malformed_row(self):
        self.assertNotIn("11111111-1111-4111-8111-111111111109", self.by_gers_id)

    def test_bad_confidence_row_dropped_at_parse_time(self):
        self.assertNotIn("11111111-1111-4111-8111-111111111110", self.by_gers_id)

    def test_malformed_row_bucket_counts_blank_state_and_bad_parse(self):
        # Two malformed rows: one bad confidence cell (parse-time), one
        # blank state cell (price-assignment-time, unresolvable region).
        self.assertEqual(self.report.bucket_counts["malformed_row"], 2)

    def test_hygiene_bucket_counts_plus_kept_count_equal_input_row_count(self):
        total = sum(self.report.bucket_counts.values()) + self.report.kept_count
        self.assertEqual(total, self.report.input_row_count)

    def test_chain_alias_brand_matched_row_kept(self):
        self.assertIn("11111111-1111-4111-8111-111111111102", self.by_gers_id)

    def test_no_alias_plain_fuel_row_kept(self):
        self.assertIn("11111111-1111-4111-8111-111111111103", self.by_gers_id)

    def test_truck_category_row_kept(self):
        self.assertIn("11111111-1111-4111-8111-111111111101", self.by_gers_id)

    def test_every_kept_row_price_equals_region_baseline_with_zero_observations(self):
        for row in self.station_rows:
            region = regions.region_for_state(row.state)
            expected = regions.BASELINE_VALUES[region]
            self.assertEqual(row.retail_price, expected)
            self.assertEqual(row.price_min, expected)
            self.assertEqual(row.price_max, expected)
            self.assertEqual(row.observation_count, 0)

    def test_every_kept_row_carries_correct_provenance(self):
        for row in self.station_rows:
            self.assertEqual(row.geocode_precision, "rooftop")
            self.assertEqual(row.geocode_status, "ok")
            self.assertEqual(row.price_source, "eia_regional_estimate")
            self.assertEqual(row.source, "overture")

    def test_every_minted_opis_id_is_an_overture_id(self):
        for row in self.station_rows:
            self.assertTrue(overture_scope.is_overture_id(row.opis_id))

    def test_kept_rows_span_at_least_three_regions_including_california(self):
        regions_seen = {regions.region_for_state(row.state) for row in self.station_rows}
        self.assertGreaterEqual(len(regions_seen), 3)
        self.assertIn("CALIFORNIA", regions_seen)

    def test_output_row_order_is_sorted_by_opis_id(self):
        ids = [row.opis_id for row in self.station_rows]
        self.assertEqual(ids, sorted(ids))


class OvertureIdCollisionRaiseTests(SimpleTestCase):
    """Feeds `overture_scope.COLLISION_WITNESS_PAIR` through
    `mint_identities` and asserts it raises, naming both GERS ids."""

    def _witness_row(self, gers_id, address_freeform, state="TX"):
        return overture.OvertureRow(
            gers_id=gers_id,
            name="Witness Station",
            brand_name="",
            address_freeform=address_freeform,
            address_locality="Witness City",
            address_region=state,
            address_postcode="75201",
            category="gas_station",
            confidence=0.9,
            operating_status="open",
            longitude=Decimal("-96.797"),
            latitude=Decimal("32.7767"),
        )

    def test_collision_witness_pair_raises_naming_both_gers_ids(self):
        gers_a, gers_b = overture_scope.COLLISION_WITNESS_PAIR
        row_a = self._witness_row(gers_a, "1 Witness Rd")
        row_b = self._witness_row(gers_b, "2 Witness Rd")

        with self.assertRaises(overture.OvertureIdCollisionError) as ctx:
            overture.mint_identities([row_a, row_b])

        message = str(ctx.exception)
        self.assertIn(gers_a, message)
        self.assertIn(gers_b, message)
        self.assertIn(str(overture_scope.COLLISION_WITNESS_MINTED_ID), message)


class OvertureTransformDeterminismTests(SimpleTestCase):
    """Whole-file and identifier determinism, disjointness against the real
    committed OPIS dataset, and dry-run behaviour -- everything provable
    without the real Overture extract or the corridor/DP machinery. Dedup
    runs against the empty existing-dataset fixture throughout, so it is a
    deliberate no-op here and every assertion below is unaffected by
    plan 22-11's new stage."""

    def test_whole_file_determinism_station_csv_and_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_a, report_a, decisions_a = (
                tmp_path / "a.csv", tmp_path / "a-report.md", tmp_path / "a-decisions.csv"
            )
            out_b, report_b, decisions_b = (
                tmp_path / "b.csv", tmp_path / "b-report.md", tmp_path / "b-decisions.csv"
            )

            call_command(
                "import_overture_stations",
                input_path=str(FIXTURE_PATH),
                existing_path=str(EMPTY_EXISTING_PATH),
                output_path=str(out_a),
                report_path=str(report_a),
                decisions_path=str(decisions_a),
            )
            call_command(
                "import_overture_stations",
                input_path=str(FIXTURE_PATH),
                existing_path=str(EMPTY_EXISTING_PATH),
                output_path=str(out_b),
                report_path=str(report_b),
                decisions_path=str(decisions_b),
            )

            # Byte-identity asserted on raw file bytes, not parsed content --
            # now across all three artifacts, the per-decision CSV included.
            self.assertEqual(out_a.read_bytes(), out_b.read_bytes())
            self.assertEqual(report_a.read_bytes(), report_b.read_bytes())
            self.assertEqual(decisions_a.read_bytes(), decisions_b.read_bytes())

    def test_identifier_determinism_across_two_independent_runs(self):
        rows_a, _, _ = _read_fixture_rows()
        rows_b, _, _ = _read_fixture_rows()
        ids_a = {row.gers_id: row.opis_id for row in rows_a}
        ids_b = {row.gers_id: row.opis_id for row in rows_b}
        self.assertEqual(ids_a, ids_b)

    def test_minted_ids_disjoint_from_real_committed_opis_ids(self):
        station_rows, _, _ = _read_fixture_rows()
        with open(STATIONS_GEOCODED_PATH, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            real_opis_ids = {int(row["opis_id"]) for row in reader}

        for row in station_rows:
            self.assertTrue(overture_scope.is_overture_id(row.opis_id))
            self.assertNotIn(row.opis_id, real_opis_ids)

    def test_dry_run_writes_reports_and_leaves_station_output_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_path = tmp_path / "stations.csv"
            report_path = tmp_path / "report.md"
            decisions_path = tmp_path / "decisions.csv"

            call_command(
                "import_overture_stations",
                input_path=str(FIXTURE_PATH),
                existing_path=str(EMPTY_EXISTING_PATH),
                output_path=str(out_path),
                report_path=str(report_path),
                decisions_path=str(decisions_path),
                dry_run=True,
            )

            self.assertFalse(out_path.exists())
            self.assertTrue(report_path.exists())
            self.assertIn("Kept:", report_path.read_text(encoding="utf-8"))
            # Both committed artifacts land on every run, dry or not --
            # that is what makes --dry-run useful for reviewing a dedup
            # pass before anything is committed.
            self.assertTrue(decisions_path.exists())

    def test_without_dry_run_writes_station_csv_with_export_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_path = tmp_path / "stations.csv"
            report_path = tmp_path / "report.md"
            decisions_path = tmp_path / "decisions.csv"

            call_command(
                "import_overture_stations",
                input_path=str(FIXTURE_PATH),
                existing_path=str(EMPTY_EXISTING_PATH),
                output_path=str(out_path),
                report_path=str(report_path),
                decisions_path=str(decisions_path),
            )

            self.assertTrue(out_path.exists())
            with open(out_path, newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                header = next(reader)
                self.assertEqual(header, overture.EXPORT_HEADER)


class OvertureDedupeReportTests(SimpleTestCase):
    """Covers the dedup stage end to end through the command, against
    `existing_stations_sample.csv` -- a small existing-dataset fixture
    carrying one deliberate tight-tier match (gers_id ...101, coincident
    with an existing rooftop row) and one deliberate city-tier match
    (gers_id ...102, brand+city+state matching an existing city-centroid
    row hundreds of miles from its own coordinates)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(cls.tmp.name)
        cls.report_path = tmp_path / "report.md"
        cls.decisions_path = tmp_path / "decisions.csv"

        call_command(
            "import_overture_stations",
            input_path=str(FIXTURE_PATH),
            existing_path=str(SAMPLE_EXISTING_PATH),
            output_path=str(tmp_path / "stations.csv"),
            report_path=str(cls.report_path),
            decisions_path=str(cls.decisions_path),
            dry_run=True,
        )

        cls.report_text = cls.report_path.read_text(encoding="utf-8")
        with open(cls.decisions_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            cls.decision_rows = list(reader)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        super().tearDownClass()

    def test_deliberate_tight_and_city_matches_land_as_dropped(self):
        by_gers_id = {row["gers_id"]: row for row in self.decision_rows}
        self.assertEqual(
            by_gers_id["11111111-1111-4111-8111-111111111101"]["decision"], "dropped"
        )
        self.assertEqual(
            by_gers_id["11111111-1111-4111-8111-111111111101"]["tier"], "tight"
        )
        self.assertEqual(
            by_gers_id["11111111-1111-4111-8111-111111111102"]["decision"], "dropped"
        )
        self.assertEqual(
            by_gers_id["11111111-1111-4111-8111-111111111102"]["tier"], "city"
        )

    def test_per_decision_row_count_equals_kept_plus_dropped(self):
        _, report, decisions = _read_fixture_rows(
            overture_dedupe.load_existing_rows(
                csv.DictReader(open(SAMPLE_EXISTING_PATH, newline="", encoding="utf-8"))
            )
        )
        self.assertEqual(len(self.decision_rows), len(decisions))
        kept = sum(1 for row in self.decision_rows if row["decision"] == "kept")
        dropped = sum(1 for row in self.decision_rows if row["decision"] == "dropped")
        self.assertEqual(kept + dropped, len(self.decision_rows))
        self.assertEqual(kept, report.no_match_count)

    def test_markdown_contains_all_six_required_section_headers(self):
        for header in (
            "## Source",
            "## Hygiene exclusions",
            "## Dedup",
            "## Spot-checked clusters",
            "## Result",
            "## Forward risk",
        ):
            self.assertIn(header, self.report_text)

    def test_markdown_states_unknown_status_retained_count_as_an_integer(self):
        lines = [
            line for line in self.report_text.splitlines() if "unknown" in line.lower()
        ]
        self.assertEqual(len(lines), 1)
        trailing = lines[0].rsplit(":", 1)[1].strip()
        self.assertTrue(trailing.isdigit())
        # The fixture carries exactly one row with a blank operating_status.
        self.assertEqual(trailing, "1")

    def test_markdown_per_tier_counts_equal_counts_derived_from_decisions(self):
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
        no_match_from_decisions = sum(
            1 for row in self.decision_rows if row["decision"] == "kept"
        )

        self.assertIn(f"Tight-tier matches (rooftop-precision existing rows): {tight_from_decisions}", self.report_text)
        self.assertIn(f"City-tier matches (city-centroid existing rows, brand+city+state): {city_from_decisions}", self.report_text)
        self.assertIn(f"No match (kept as new): {no_match_from_decisions}", self.report_text)
        # And the deliberate fixture design: exactly one of each tier.
        self.assertEqual(tight_from_decisions, 1)
        self.assertEqual(city_from_decisions, 1)


def _serialize_plan(plan):
    """A stable serialization for comparing two `FuelPlan`s field by field
    (D-22 check 3) -- asserted on this, never on object identity, since two
    independently-solved `FuelPlan` instances are never the same object
    even when every field matches."""
    return {
        "stops": [
            (
                stop.opis_id,
                stop.name,
                str(stop.gallons),
                str(stop.price_per_gallon),
            )
            for stop in plan.stops
        ],
        "total_cost": str(plan.total_cost),
        "strategy": plan.strategy,
    }


class RealFileIdentifierProofTests(TestCase):
    """Criterion 3's three identifier checks, proven against the real
    committed files and the real corridor/DP machinery (D-22) -- the
    fixture-scale proofs in `OvertureTransformDeterminismTests` above prove
    the identical properties at fixture scale; this class is what makes the
    claim true of what actually ships.

    Uses `TestCase` (not `SimpleTestCase`, unlike every other class in this
    module) because check 3 solves against the real DB-backed
    `corridor.candidates()` path -- the same reason
    `RealCorridorDispatchTestCase` (`routing/tests/test_solver_dispatch.py`)
    does.
    """

    # sacramento_ca-salt_lake_city_ut is a member of GAP_FILL_INTERSECTING_SLUGS
    # (routing/tests/test_corridor_fixtures.py, D-37) -- its real committed
    # polyline genuinely passes through the gap-fill boxes, so a plan solved
    # against it can actually reach an Overture-id station; a slug outside
    # that set would exercise nothing new. Picked over the other three
    # intersecting slugs (san_diego_ca-jacksonville_fl and the two demo
    # chips) by direct measurement: at this tank range/vehicle combination
    # it is the plain CORRIDORS member (not a demo chip needing
    # DEMO_CHIP_VEHICLE) whose solved plan actually purchases at an
    # Overture-id station, which is what the anti-vacuity assertion below
    # requires -- san_diego_ca-jacksonville_fl's own solved plans at every
    # tank range/starting-fuel combination measured never do, despite
    # intersecting the boxes geographically. The dispatch strategy here is
    # the penalty-aware heuristic, not the exact DP (measured directly, not
    # assumed from test_solver_dispatch.py's own EXACT_DP corridor comment,
    # which was measured on the pre-Overture station set and no longer
    # holds now that the search set is larger) -- DispatchDeterminismTests
    # (test_solver_dispatch.py) already establishes that arm is exactly as
    # deterministic as the DP given fixed inputs, which is the only
    # property this check actually needs.
    PINNED_SLUG = "sacramento_ca-salt_lake_city_ut"
    PINNED_TANK_RANGE_MI = Decimal(500)
    _MPG = Decimal(10)
    _STARTING_FUEL = Decimal("0.5")
    _PENALTY = Decimal(35)

    @classmethod
    def setUpTestData(cls):
        # reseed_all()'s own canonical-list replay, via the plain
        # seed_stations default (no args) -- both CSVs, OPIS then Overture,
        # exactly as production seeds.
        call_command("seed_stations", stdout=io.StringIO())
        corridor.warm_index()

    def test_check1_transform_reproduces_the_committed_station_csv_byte_for_byte(self):
        """Criterion 3 check 1 -- determinism against the committed
        extract, at real scale. Plan 22-10's OvertureTransformDeterminismTests
        already proves this at fixture scale (two runs match each other);
        this additionally proves the COMMITTED file is what the committed
        extract itself produces, so a hand edit to either one fails here.

        Line endings are normalized on both sides before comparing, and
        that is deliberate. `csv.writer` emits CRLF on every platform,
        but the committed blob is LF-only, so what lands in the working
        tree depends on the checkout: `core.autocrlf=true` on Windows
        restores CRLF, while a Linux CI runner leaves LF. Comparing raw
        bytes therefore tested the checkout's line-ending policy rather
        than the transform, passing on a developer machine and failing on
        CI for a reason that has nothing to do with the data. Normalizing
        keeps the guarantee this test exists for -- a hand edit to either
        the committed CSV or the committed extract still fails it,
        because every field, row and row ORDER is still compared exactly.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            out_path = tmp_path / "overture_stations.csv"
            call_command(
                "import_overture_stations",
                input_path=str(RAW_EXTRACT_PATH),
                existing_path=str(STATIONS_GEOCODED_PATH),
                output_path=str(out_path),
                report_path=str(tmp_path / "report.md"),
                decisions_path=str(tmp_path / "decisions.csv"),
            )
            generated = out_path.read_bytes().replace(b"\r\n", b"\n")
            committed = OVERTURE_STATIONS_PATH.read_bytes().replace(b"\r\n", b"\n")
            self.assertEqual(generated, committed)

    def test_check2_ids_disjoint_against_the_real_committed_files(self):
        """Criterion 3 check 2 -- disjointness, asserted on the real files,
        not on generated data."""
        with open(OVERTURE_STATIONS_PATH, newline="", encoding="utf-8") as f:
            overture_ids = {int(row["opis_id"]) for row in csv.DictReader(f)}
        with open(STATIONS_GEOCODED_PATH, newline="", encoding="utf-8") as f:
            opis_ids = {int(row["opis_id"]) for row in csv.DictReader(f)}

        self.assertTrue(overture_ids, "no Overture rows found -- check is vacuous")
        self.assertTrue(opis_ids, "no OPIS rows found -- check is vacuous")
        self.assertTrue(all(overture_scope.is_overture_id(i) for i in overture_ids))
        self.assertTrue(
            all(not overture_scope.is_overture_id(i) for i in opis_ids)
        )
        self.assertEqual(overture_ids & opis_ids, set())

    def test_check3_pinned_route_solves_identically_across_two_independent_imports(
        self,
    ):
        """Criterion 3 check 3 -- the one checks 1 and 2 cannot substitute
        for: `opis_id` is the DP's third tie-break key
        (`routing.services.dp`), so an id that silently moves across a
        re-import can change which plan a route gets even when the CSV
        content is otherwise identical. Solves the pinned route once
        against the committed station set, once against a station set
        whose Overture half was independently rebuilt from the committed
        extract into a temp directory, and asserts the two plans match
        field by field."""
        factor_for = factor_lookup_for_basis("neutral")
        route = load_corridor_route(self.PINNED_SLUG)

        candidates_first = corridor.candidates(route, factor_for=factor_for)
        plan_first = solve(
            candidates_first,
            route.total_route_mi,
            tank_range_mi=self.PINNED_TANK_RANGE_MI,
            mpg=self._MPG,
            starting_fuel=self._STARTING_FUEL,
            penalty=self._PENALTY,
            trust_margin=Decimal(0),
        )

        # Anti-vacuity: without this, a route that never reaches a new
        # station would pass this whole check while proving nothing about
        # the new ids -- the same vacuity class as a referee-invariance
        # check aimed at a file that does not exist.
        self.assertTrue(
            any(
                overture_scope.is_overture_id(stop.opis_id)
                for stop in plan_first.stops
            ),
            "pinned route's first-import plan reaches no Overture-id "
            "station -- this check would prove nothing about the new ids",
        )

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            rebuilt_overture_csv = tmp_path / "overture_stations.csv"
            call_command(
                "import_overture_stations",
                input_path=str(RAW_EXTRACT_PATH),
                existing_path=str(STATIONS_GEOCODED_PATH),
                output_path=str(rebuilt_overture_csv),
                report_path=str(tmp_path / "report.md"),
                decisions_path=str(tmp_path / "decisions.csv"),
            )
            # Reseeds the DB from the OPIS file plus the FRESHLY rebuilt
            # Overture file -- seed_stations' own handle() calls
            # reset_index()/reset_dataset_vintage_token() at the end, so no
            # separate cache-busting call is needed here.
            call_command(
                "seed_stations",
                str(STATIONS_GEOCODED_PATH),
                str(rebuilt_overture_csv),
                stdout=io.StringIO(),
            )

        candidates_second = corridor.candidates(route, factor_for=factor_for)
        plan_second = solve(
            candidates_second,
            route.total_route_mi,
            tank_range_mi=self.PINNED_TANK_RANGE_MI,
            mpg=self._MPG,
            starting_fuel=self._STARTING_FUEL,
            penalty=self._PENALTY,
            trust_margin=Decimal(0),
        )

        self.assertEqual(_serialize_plan(plan_first), _serialize_plan(plan_second))
