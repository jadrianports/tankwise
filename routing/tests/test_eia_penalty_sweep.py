"""Pinned parameters for the EIA x penalty coupling sweep (D-23..D-27).

STATE.md carries a standing `[Unknown]` blocker: `corridor.py`'s
`factor_for` scales every candidate's per-gallon price by the EIA regional
multiplier, while the fixed `$35` fuel-stop penalty does not -- so
effective stop-aggressiveness shifts with EIA state, and that shift was
never characterised as a deliberate finding. This module closes that gap.

`EIA_MULTIPLIER_LADDER`, `EIA_SWING_STATED` and
`EIA_SWING_VERDICT_MAX_STOP_DELTA` are pinned here, in their own commit,
BEFORE any measurement command or guard exists in this file and before
the sweep has ever been run -- the timestamp of that commit is the proof
the verdict rule was fixed before the numbers were seen (D-25). Neither
`measure_eia_penalty_sweep` nor `EiaPenaltyCouplingGuardTests` (added in a
later commit) defines a copy of its own: both import these three names
from this module, the single shared source of truth, mirroring the
CORPUS_PARAMS / OBJECTIVE_PARAMS discipline Phases 16-18 have used
throughout.

The sweep applies its multiplier ladder SYNTHETICALLY, on top of
`routing.tests.test_corridor_fixtures.factor_lookup_for_basis` -- it scales
each already-built `Candidate`'s `price_per_gallon` after `corridor.py`
has done its own normal EIA-factor application, never by feeding a
fabricated EIA response through `eia.py`'s parser. `routing/services/eia.py`
and `routing/services/corridor.py` are byte-unchanged this phase: the
coupling is characterised here, not modified there.

Scaling the fixed-charge penalty by the same EIA factor (making it
real-terms constant) is D-25's named candidate fix if the measured swing
exceeds the pinned threshold below. It is explicitly NOT implemented in
this plan, under any circumstance -- at most a v4.0 consideration.
"""
from decimal import Decimal

# D-24's synthetic ladder, pinned before any measurement code exists in
# this file.
#
# A synthetic sweep was chosen over comparing only the two real price
# bases this codebase already has ("neutral" and "eia_fixture") because
# two bases give only two points on the curve -- and `eia._frozen_table()`
# already returns a flat 1.0 factor for every region, so "neutral" and the
# codebase's own frozen-degradation mode are literally the same
# configuration, not an independent second data point. A four-rung sweep
# exposes the *shape* of the coupling (monotonic? linear? threshold-y?),
# which is what a written-down finding needs, not just its sign.
#
# The rungs bracket the real variation this codebase's own committed
# fixture observes: replaying `routing/tests/fixtures/eia_response.json`
# through `eia._parse_eia_response` and dividing each region's current
# value by its `regions.BASELINE_VALUES` denominator yields eight
# region factors ranging from ~1.414 (CALIFORNIA) to ~1.546 (PADD3) --
# every region in that one committed snapshot happens to sit above its
# own baseline. The 1.5 rung sits at the top of that observed band; 0.8,
# 1.0 and 1.2 extend the ladder below 1.0 so the sweep also characterises
# the below-baseline (cheaper-than-baseline diesel) direction the fixture
# itself does not capture, since D-23's finding needs both directions of
# the coupling stated, not only the one the fixture happens to show.
EIA_MULTIPLIER_LADDER = (
    Decimal("0.8"),
    Decimal("1.0"),
    Decimal("1.2"),
    Decimal("1.5"),
)

# D-25's "stated EIA swing" -- the two ladder endpoints across which
# stop-count movement is judged for the verdict below.
EIA_SWING_STATED = (Decimal("0.8"), Decimal("1.5"))

# D-25's verdict rule, fixed here, before the sweep has ever been run: if
# stop counts across the twelve corridors move by AT MOST ONE stop across
# the stated EIA swing (EIA_SWING_STATED), the coupling is ratified as
# correct-as-designed and documented as such. If they move by MORE than
# one stop, it is instead recorded as needing follow-up, with the
# candidate fix named explicitly: scale the fixed-charge penalty by the
# same EIA factor, making it real-terms constant rather than a flat
# dollar amount that means less when diesel is expensive and more when
# it is cheap. Whichever branch the measurement lands on, it is applied
# exactly as written here -- neither branch's wording is softened, no
# third branch is introduced, and the ladder/swing/threshold are never
# adjusted after seeing the measured numbers. A measured delta landing
# exactly on this threshold takes the ratifying branch: "at most one
# stop" is inclusive of one.
EIA_SWING_VERDICT_MAX_STOP_DELTA = 1
