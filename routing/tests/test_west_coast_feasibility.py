"""Criterion 1's own regression test (OVRT-04): does a driver planning
Seattle -> San Diego receive a valid fueling plan instead of
`infeasible_route`?

This module's headline claim is pinned at solve()'s own signature default
vehicle (10 mpg / 500 mi tank / starting_fuel=1) -- NOT at
`WEST_COAST_PROBE_VEHICLE`, the SPA hero preset plan 22-06 registered
(6.5 mpg / 1,050 mi tank). That vehicle's tank already spans every gap the
Overture import closes: three of the four `WEST_COAST_PROBES` need zero
stops at it regardless of station density, and `seattle_wa-san_diego_ca`
was already feasible before the import. A regression test pinned there
would pass identically whether `data/overture_stations.csv` exists or not
-- vacuous, in exactly the sense this project's `prune(x)->x` discipline
exists to catch. This finding was recorded mid-execution in
`.planning/phases/22-overture-gap-fill-import-dispatch-re-pin-go-live/
22-PROBE-VEHICLE-SENSITIVITY.md` ("DECIDED 2026-08-08 by the developer --
binding on plan 22-13") and its contract amendment is what this module
implements: keep the demo-vehicle verdicts as documentation, add the
falsifiable claim at solve()'s default vehicle, and assert on candidate
count and maximum inter-station gap, not only the binary verdict.

Every verdict asserted here is recorded, with full measured detail, in
`.planning/phases/22-overture-gap-fill-import-dispatch-re-pin-go-live/
22-PROBE-AFTER.txt`, alongside the BEFORE figures in `22-PROBE-BEFORE.txt`.

Criterion 1's prohibition binds this whole module: feasibility only, no
comparative pricing claim of any kind. No test here asserts an exact
`total_cost` value, or a stop count -- those are dispatch-dependent (plan
22-14's own diff) and pinning one would itself be the comparative claim
this phase forbids.
"""

from decimal import Decimal

from django.conf import settings

from routing.models import Station
from routing.pipeline.overture_scope import is_overture_id
from routing.serializers import RouteResponseSerializer
from routing.services import corridor
from routing.services.solver import solve
from routing.tests.test_corridor_fixtures import (
    WEST_COAST_PROBE_VEHICLE,
    WEST_COAST_PROBES,
    factor_lookup_for_basis,
    load_west_coast_probe_route,
)
from routing.tests.test_solver_dispatch import RealCorridorDispatchTestCase

# solve()'s own signature default (routing/services/solver.py's own
# tank_range_mi=Decimal(500), mpg=Decimal(10), starting_fuel=Decimal(1)
# defaults) -- the vehicle this module's falsifiable claim is pinned to.
# Deliberately a SEPARATELY NAMED constant, not a bare literal at each call
# site, following this codebase's own "named here explicitly so the
# vehicles are never conflated" discipline (see WEST_COAST_PROBE_VEHICLE's
# own comment in test_corridor_fixtures.py).
DEFAULT_SOLVE_VEHICLE = {
    "mpg": Decimal(10),
    "tank_range_mi": Decimal(500),
    "starting_fuel": Decimal(1),
    "price_basis": "neutral",
}

# BEFORE figures, transcribed verbatim from 22-PROBE-BEFORE.txt and
# 22-PROBE-VEHICLE-SENSITIVITY.md, never re-derived from a live run --
# what this module's assertions are measured against.
_BEFORE_SEATTLE_SAN_DIEGO_RAW_CANDIDATES = 24
_BEFORE_SEATTLE_SAN_DIEGO_MAX_GAP_MI = Decimal("991.8229512830747197207476387")
_BEFORE_LOS_ANGELES_EUGENE_RAW_CANDIDATES = 0


def _probe_route_and_candidates(slug, vehicle):
    route = load_west_coast_probe_route(slug)
    factor_for = factor_lookup_for_basis(vehicle["price_basis"])
    candidates = corridor.candidates(route, factor_for=factor_for)
    return route, candidates


def _max_inter_station_gap_mi(route, candidates):
    """Return the largest consecutive gap in miles among START (0), each
    candidate's `distance_from_start_mi` in position order, and FINISH
    (`route.total_route_mi`). A read-only walk over the same ordering
    `dp.preflight_gap_check` uses, kept independent of it so this module
    can assert on the underlying geometry directly rather than only on
    the binary feasible/infeasible outcome (22-PROBE-VEHICLE-SENSITIVITY.md
    item 3)."""
    ordered = sorted(candidates, key=lambda c: (c.distance_from_start_mi, c.opis_id))
    positions = (
        [Decimal(0)]
        + [c.distance_from_start_mi for c in ordered]
        + [route.total_route_mi]
    )
    return max(positions[i + 1] - positions[i] for i in range(len(positions) - 1))


class WestCoastFeasibilityRegressionTests(RealCorridorDispatchTestCase):
    """Extends `RealCorridorDispatchTestCase` (`test_solver_dispatch.py`)
    for its real-dataset seeding, which replays BOTH canonical station CSVs
    -- `data/stations_geocoded.csv` and `data/overture_stations.csv` -- so
    every test method here runs against the real, landed gap-fill import,
    not a synthetic fixture."""

    def _solve_probe(self, slug, vehicle):
        route, candidates = _probe_route_and_candidates(slug, vehicle)
        plan = solve(
            candidates,
            route.total_route_mi,
            tank_range_mi=vehicle["tank_range_mi"],
            mpg=vehicle["mpg"],
            starting_fuel=vehicle["starting_fuel"],
            penalty=settings.FUEL_STOP_PENALTY_USD,
        )
        return route, candidates, plan

    # ------------------------------------------------------------------
    # The falsifiable claim: solve()'s own default vehicle.
    # ------------------------------------------------------------------

    def test_seattle_to_san_diego_is_feasible_at_solve_default_vehicle(self):
        """Criterion 1 / OVRT-04's own headline claim, pinned at solve()'s
        signature default (10 mpg / 500 mi tank / starting_fuel=1) per this
        plan's binding amendment -- NOT at WEST_COAST_PROBE_VEHICLE (see
        module docstring). At this vehicle, seattle_wa-san_diego_ca was
        measured INFEASIBLE before the Overture import (gap of 992 mi
        between CHEVRON #383766 and FINISH, 22-PROBE-AFTER.txt Section B)
        and is asserted feasible here, after it.

        The Overture-stop assertion is load-bearing: without it this test
        could pass for a reason unrelated to the gap-fill import and would
        not be a regression test for this change at all.
        """
        route, candidates, plan = self._solve_probe(
            "seattle_wa-san_diego_ca", DEFAULT_SOLVE_VEHICLE
        )

        self.assertGreater(len(plan.stops), 0)
        self.assertGreater(plan.total_cost, 0)

        overture_stop_ids = [
            stop.opis_id for stop in plan.stops if is_overture_id(stop.opis_id)
        ]
        self.assertGreater(
            len(overture_stop_ids),
            0,
            "No selected stop is an Overture row -- this plan does not "
            "provably depend on the gap-fill import.",
        )

    def test_seattle_to_san_diego_candidate_count_and_gap_close_at_default_vehicle(
        self,
    ):
        """Candidate-count and maximum-inter-station-gap assertions
        (22-PROBE-VEHICLE-SENSITIVITY.md item 3): the binary feasible/
        infeasible verdict alone does not show the import moved anything
        measurable -- these two numbers do. `corridor.candidates()` does
        not depend on the vehicle, so these figures hold at any vehicle."""
        route, candidates = _probe_route_and_candidates(
            "seattle_wa-san_diego_ca", DEFAULT_SOLVE_VEHICLE
        )

        self.assertGreater(len(candidates), _BEFORE_SEATTLE_SAN_DIEGO_RAW_CANDIDATES)

        max_gap = _max_inter_station_gap_mi(route, candidates)
        self.assertLess(max_gap, DEFAULT_SOLVE_VEHICLE["tank_range_mi"])
        self.assertLess(max_gap, _BEFORE_SEATTLE_SAN_DIEGO_MAX_GAP_MI)

    def test_los_angeles_to_eugene_candidate_count_and_gap_close_at_default_vehicle(
        self,
    ):
        """The single clearest measurable signal the import produced
        anything (22-PROBE-VEHICLE-SENSITIVITY.md item 3): this probe had
        ZERO raw candidates before the import, at any vehicle -- the whole
        857-mile route was the gap."""
        route, candidates = _probe_route_and_candidates(
            "los_angeles_ca-eugene_or", DEFAULT_SOLVE_VEHICLE
        )

        self.assertGreater(
            len(candidates), _BEFORE_LOS_ANGELES_EUGENE_RAW_CANDIDATES
        )

        max_gap = _max_inter_station_gap_mi(route, candidates)
        self.assertLess(max_gap, route.total_route_mi)

    def test_los_angeles_to_eugene_is_feasible_at_solve_default_vehicle(self):
        """AFTER verdict at the default vehicle (22-PROBE-AFTER.txt Section
        B): this probe spans BOTH gap-fill boxes and was measured
        INFEASIBLE before the import (gap of 857 mi, the whole route, zero
        candidates). Every selected stop in the AFTER plan is Overture-
        sourced."""
        route, candidates, plan = self._solve_probe(
            "los_angeles_ca-eugene_or", DEFAULT_SOLVE_VEHICLE
        )

        self.assertGreater(len(plan.stops), 0)
        self.assertGreater(plan.total_cost, 0)

        overture_stop_ids = [
            stop.opis_id for stop in plan.stops if is_overture_id(stop.opis_id)
        ]
        self.assertGreater(len(overture_stop_ids), 0)

    def test_san_francisco_to_seattle_is_feasible_at_solve_default_vehicle(self):
        """AFTER verdict at the default vehicle (22-PROBE-AFTER.txt Section
        B): measured INFEASIBLE before the import (gap of 547 mi from
        START to the first station)."""
        route, candidates, plan = self._solve_probe(
            "san_francisco_ca-seattle_wa", DEFAULT_SOLVE_VEHICLE
        )

        self.assertGreater(len(plan.stops), 0)
        self.assertGreater(plan.total_cost, 0)

    def test_portland_to_sacramento_is_feasible_at_solve_default_vehicle(self):
        """AFTER verdict at the default vehicle (22-PROBE-AFTER.txt Section
        B): this corridor was ALREADY feasible before the import (interior
        of the northern gap-fill band, end to end) and stays feasible
        after it -- an inverted guard rather than an omission, so a future
        regression on this corridor is caught even though it is not
        evidence the import changed anything here."""
        route, candidates, plan = self._solve_probe(
            "portland_or-sacramento_ca", DEFAULT_SOLVE_VEHICLE
        )

        self.assertGreater(len(plan.stops), 0)
        self.assertGreater(plan.total_cost, 0)

    # ------------------------------------------------------------------
    # Demo-vehicle verdicts: documentation of what the SPA hero preset
    # already does. NOT the falsifiable claim -- see module docstring.
    # ------------------------------------------------------------------

    def test_every_probe_stays_feasible_at_the_demo_vehicle(self):
        """DOCUMENTATION ONLY. At WEST_COAST_PROBE_VEHICLE (the SPA hero
        preset, 6.5 mpg / 1,050 mi tank), all four probes were ALREADY
        feasible before the Overture import (22-PROBE-BEFORE.txt's own
        headline finding) and remain feasible after it. This assertion is
        NOT falsifiable against the gap-fill -- reverting
        data/overture_stations.csv would not change this outcome, since
        every probe's tank range already spans its own gaps at this
        vehicle. See the solve-default-vehicle tests above for the actual
        falsifiable claim this plan's regression rests on."""
        for probe in WEST_COAST_PROBES:
            with self.subTest(slug=probe.slug):
                route, candidates, plan = self._solve_probe(
                    probe.slug, WEST_COAST_PROBE_VEHICLE
                )
                # No InfeasibleRouteError raised is itself the assertion;
                # plan.total_cost may legitimately be zero here (three of
                # the four probes need no stop at this tank range), so
                # total_cost is deliberately NOT asserted positive in this
                # documentation-only block.
                self.assertIsNotNone(plan)

    # ------------------------------------------------------------------
    # D-43: Overture stations render indistinguishably from OPIS ones.
    # ------------------------------------------------------------------

    def test_candidate_stations_render_identical_keys_for_overture_and_opis_rows(
        self,
    ):
        """D-43, confirmed by observation rather than by change: solves a
        probe whose corridor carries BOTH OPIS and Overture candidates
        through the real `RouteResponseSerializer` and asserts every
        rendered `candidate_stations[]` entry -- Overture-sourced or
        OPIS-sourced alike -- carries exactly the same key set. No new
        field, colour, rank, or legend is added anywhere in this module;
        the two-tier visual split is an explicit v4.0 Future Requirement
        this phase does not ship."""
        route, candidates = _probe_route_and_candidates(
            "seattle_wa-san_diego_ca", DEFAULT_SOLVE_VEHICLE
        )
        opis_candidates = [c for c in candidates if not is_overture_id(c.opis_id)]
        overture_candidates = [c for c in candidates if is_overture_id(c.opis_id)]
        self.assertGreater(len(opis_candidates), 0)
        self.assertGreater(len(overture_candidates), 0)

        station_rows = Station.objects.filter(
            opis_id__in=[c.opis_id for c in candidates]
        ).values("opis_id", "latitude", "longitude")
        candidate_coords = {
            row["opis_id"]: {
                "latitude": row["latitude"],
                "longitude": row["longitude"],
            }
            for row in station_rows
        }

        _, _, plan = self._solve_probe(
            "seattle_wa-san_diego_ca", DEFAULT_SOLVE_VEHICLE
        )
        instance = {"route": route, "plan": plan, "map_url": ""}
        context = {"candidates": candidates, "candidate_coords": candidate_coords}

        data = RouteResponseSerializer(instance, context=context).data
        rendered = data["candidate_stations"]
        self.assertGreater(len(rendered), 0)

        opis_ids = {c.opis_id for c in opis_candidates}
        overture_ids = {c.opis_id for c in overture_candidates}
        opis_entries = [e for e in rendered if e["station_id"] in opis_ids]
        overture_entries = [e for e in rendered if e["station_id"] in overture_ids]
        self.assertGreater(len(opis_entries), 0)
        self.assertGreater(len(overture_entries), 0)

        expected_keys = set(rendered[0].keys())
        for entry in rendered:
            self.assertEqual(set(entry.keys()), expected_keys)
