"""Regenerate `places_sample.parquet` with two derived columns,
`basic_category` and `taxonomy`, ahead of `fetch_overture_extract`'s
migration off the deprecated Overture `categories` field (D-17/D-24,
phase 23 plan 02).

A standalone script, not a management command and not a test:
- It does not live under `routing/management/commands/` -- that tree is
  scanned by `DuckdbModuleScopeImportGuardTests`
  (routing/tests/test_fetch_overture_extract.py) for module-scope Parquet
  toolchain imports reachable from `wsgi.py`/`manage.py` startup.
  `routing/tests/` is the one tree that scan skips, which is what makes
  the module-scope `import duckdb` below legal here and nowhere else in
  this repository (compare `fetch_overture_extract.handle()`'s lazy,
  function-body-only import).
- Its filename does not start with `test`, so Django's default test
  discovery (`test*.py`) never collects it.

This fixture's ten rows are hand-designed synthetic rows (`22-07-SUMMARY.md`:
one per operating status value, one mojibake-named row, one alt-fuel-only
named row, one below the confidence floor, one out-of-category, one
out-of-bbox), not a sample of real Overture data. Deriving `basic_category`
and `taxonomy` from each row's own existing `categories.primary` value is
therefore a fixture-construction choice, not a claim about what upstream
Overture's real taxonomy struct contains for these rows -- it reproduces the
*shape* research recorded live against the pinned release, not sourced
content.

Derivation, per row:
  - `taxonomy.primary`  = the row's own `categories.primary`, verbatim.
  - `taxonomy.hierarchy` = a one-element VARCHAR array holding that same
    value.
  - `taxonomy.alternates` = an empty, explicitly-typed VARCHAR array.
  - `basic_category` = `overture_scope.CATEGORY_FILTER[0]` ("gas_station")
    when the row's category is a member of `overture_scope.CATEGORY_FILTER`,
    else the row's own category value -- mirroring Overture's real
    basic-category bucketing, where both `gas_station` and
    `truck_gas_station` collapse into the coarser `gas_station` bucket.

To run it: install the offline toolchain into the project venv
(`pip install -r requirements-offline.txt`), run this script, then
uninstall duckdb again -- the same protocol Phase 22 used for this exact
fixture (see `22-07-SUMMARY.md`).

    .venv/Scripts/python.exe routing/tests/fixtures/overture/build_places_sample.py
"""
import argparse
import sys
from pathlib import Path

import duckdb

# This script's own directory is not on sys.path when run directly; add the
# repo root so `routing.pipeline.overture_scope` (a plain, Django-free
# module) resolves the same way it does for every other consumer.
_REPO_ROOT = Path(__file__).resolve().parents[4]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from routing.pipeline import overture_scope  # noqa: E402

DEFAULT_FIXTURE_PATH = Path(__file__).resolve().parent / "places_sample.parquet"

# The two columns this script adds. Also the refusal check's own vocabulary:
# a second run against an already-migrated fixture must refuse, not
# double-append a third copy of either column.
NEW_COLUMNS = ("basic_category", "taxonomy")


def _existing_columns(con, path: Path) -> set:
    described = con.execute(
        f"DESCRIBE SELECT * FROM read_parquet('{path.as_posix()}')"
    ).fetchall()
    return {row[0] for row in described}


def build(input_path: Path, output_path: Path) -> None:
    con = duckdb.connect()
    con.execute("INSTALL spatial; LOAD spatial;")

    already_present = _existing_columns(con, input_path) & set(NEW_COLUMNS)
    if already_present:
        print(
            f"Refusing to run: {input_path} already carries "
            f"{sorted(already_present)}. This script must not be run a "
            "second time against an already-migrated fixture -- restore "
            "the pre-migration fixture first (e.g. `git checkout -- "
            f"{input_path}`).",
            file=sys.stderr,
        )
        sys.exit(1)

    category_filter_sql = ", ".join(f"'{c}'" for c in overture_scope.CATEGORY_FILTER)
    basic_category_value = overture_scope.CATEGORY_FILTER[0]

    # Write to a temp path and swap in, rather than writing `output_path`
    # directly, so an input-equals-output run (the default invocation)
    # never reads and writes the same file inside one DuckDB statement.
    tmp_output = output_path.with_suffix(".tmp.parquet")
    sql = (
        "COPY (SELECT *, "
        "CASE WHEN categories.primary IN "
        f"({category_filter_sql}) THEN '{basic_category_value}' "
        "ELSE categories.primary END AS basic_category, "
        "{'primary': categories.primary, "
        "'hierarchy': [categories.primary], "
        "'alternates': CAST([] AS VARCHAR[])} AS taxonomy "
        f"FROM read_parquet('{input_path.as_posix()}')) "
        f"TO '{tmp_output.as_posix()}' (FORMAT PARQUET)"
    )
    con.execute(sql)
    tmp_output.replace(output_path)

    columns = [row[0] for row in _describe(con, output_path)]
    row_count = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{output_path.as_posix()}')"
    ).fetchone()[0]
    print(f"Wrote {output_path}")
    print(f"Columns: {columns}")
    print(f"Row count: {row_count}")


def _describe(con, path: Path):
    return con.execute(f"DESCRIBE SELECT * FROM read_parquet('{path.as_posix()}')").fetchall()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-path",
        default=str(DEFAULT_FIXTURE_PATH),
        help="Parquet file to read. Default: the committed fixture.",
    )
    parser.add_argument(
        "--output-path",
        default=str(DEFAULT_FIXTURE_PATH),
        help="Parquet file to write. Default: the committed fixture, "
        "regenerated in place.",
    )
    args = parser.parse_args()
    build(Path(args.input_path), Path(args.output_path))


if __name__ == "__main__":
    main()
