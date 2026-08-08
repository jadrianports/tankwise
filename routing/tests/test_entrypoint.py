"""Source-level guard for entrypoint.sh's boot sequence (D-25).

`seed_stations` must run unconditionally on every container boot, not only
when the station table happens to be empty. Production's Neon database is
never empty after the first boot, so a conditional seed would let a
committed dataset change ship, pass every other test, bump the cache
prefix, and never reach a single served request (T-22-34, STRIDE:
Repudiation). This test reads the actual deployed script rather than a
description of it, so a future edit that quietly reintroduces a guard,
a flag, or an opt-out is caught here rather than discovered in production.
"""

from django.conf import settings
from django.test import SimpleTestCase

ENTRYPOINT_PATH = settings.BASE_DIR / "entrypoint.sh"


class EntrypointAlwaysSeedTests(SimpleTestCase):
    def setUp(self):
        self.entrypoint_text = ENTRYPOINT_PATH.read_text(encoding="utf-8")
        self.entrypoint_lines = self.entrypoint_text.splitlines()

    def test_exactly_one_seed_stations_invocation(self):
        occurrences = self.entrypoint_text.count("manage.py seed_stations")
        self.assertEqual(
            occurrences,
            1,
            "entrypoint.sh must invoke seed_stations exactly once",
        )

    def test_seed_invocation_is_not_inside_an_if_block(self):
        # The migrate block above the seed step is the file's only `if`
        # statement, closed by the file's only `fi`. Anything conditional
        # wrapping the seed call would have to introduce a NEW `if` between
        # that `fi` and the seed line -- so scanning exactly that span is
        # sufficient to prove the seed call was not put back behind a guard,
        # without also tripping on the migrate block's own unrelated `if`.
        fi_index = next(
            i for i, line in enumerate(self.entrypoint_lines) if line.strip() == "fi"
        )
        seed_index = next(
            i
            for i, line in enumerate(self.entrypoint_lines)
            if "manage.py seed_stations" in line
        )
        self.assertGreater(
            seed_index,
            fi_index,
            "seed_stations must appear after the migrate block's closing fi",
        )
        span = self.entrypoint_lines[fi_index + 1 : seed_index + 1]
        for line in span:
            stripped = line.strip()
            self.assertFalse(
                stripped.startswith("if ") or stripped == "if",
                f"seed_stations must not be wrapped in a conditional, found: {line!r}",
            )

    def test_no_station_row_count_variable(self):
        # A row-count guard was explicitly rejected (D-25): it misses any
        # change that keeps the count identical, such as an edited price or
        # a moved coordinate. Its absence is the source-level proof that the
        # rejected alternative was not quietly reintroduced.
        self.assertNotIn(
            "STATION_COUNT",
            self.entrypoint_text,
            "entrypoint.sh must not hold a station row count in a shell variable",
        )

    def test_migrate_block_db_migrate_host_still_present(self):
        # Anti-vacuity: a test that only asserts absences (no STATION_COUNT,
        # no wrapping if) would pass equally well on a gutted or truncated
        # file. Asserting the unrelated, untouched migrate block's
        # DB_MIGRATE_HOST conditional is still present -- twice, once in the
        # comment and once in the `if` -- proves this test actually read the
        # real file rather than an empty or corrupted one.
        self.assertGreaterEqual(
            self.entrypoint_text.count("DB_MIGRATE_HOST"),
            2,
            "the migrate block's DB_MIGRATE_HOST override must survive untouched",
        )
