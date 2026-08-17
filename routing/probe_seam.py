"""D-06's gated dispatch-verdict probe seam: a fail-soft current-RSS reader,
the probe's two HTTP header-name contract constants, and the pure,
constant-time gate-resolution function that decides whether a request may
override `solve()`'s `transition_budget=` hatch (`routing/services/solver.py`).

Deliberately NOT one of `SOLVER_FILES` (`routing/tests/test_boundaries.py`):
`read_current_rss_kb()` is an OS/proc call, and `resolve_probe_budget()`
reads a caller-supplied secret -- neither belongs inside the AST-gated pure
solver boundary. `ProbeSeamIsolationTest` (`test_boundaries.py`) is the
permanent guard that no `SOLVER_FILES` module can reach this one.

Why the RSS reader, the header-name constants, and the gate function live
together in ONE module rather than split across `routing/rss.py` (as
RESEARCH.md and PATTERNS.md suggest) plus settings: `routing/views.py`
(plan 26-06) and `routing/management/commands/probe_live_latency.py`
(plans 26-04/26-06) both need the header names, and a production view must
never import a name from `routing/tests/`. Co-locating everything the seam
needs gives one owner, keeps `views.py`'s diff to a few lines, and makes the
secret comparison unit-testable without an HTTP client.

--- The RSS reader: what it measures, honestly ---

**Why current-RSS and not a peak.** `resource.getrusage().ru_maxrss` and
`/proc/self/status`'s own `VmHWM` are both process-LIFETIME high-water
marks -- they never decrease for the life of the process. Render's gunicorn
workers recycle only every `GUNICORN_MAX_REQUESTS=500` requests
(`render.yaml`), so on a warm worker a pre/post delta computed from either
of those two reads approximately zero -- the expected steady state this
module must not report as "no measurement happened," and not a rare edge
case to special-case around.

**What this therefore is.** `read_current_rss_kb()` reads `/proc/self/status`'s
`VmRSS:` line -- the CURRENT resident set size, which does go up and down as
pages are allocated and released. A delta of two current readings taken
tightly around one DP attempt is a reasonable, slightly conservative
approximation of what that attempt allocated and retained. It is reported
into `derive_band_ceiling()`'s `peak_rss_mb` field (`test_dispatch_recovery.py`)
because that is the field's name, but it is a DELTA-OF-CURRENT, not a
genuine peak, and a future reader must not over-trust the label.

**The assumption, named as an assumption.** This is RESEARCH.md's
Assumption A1. If the delta systematically under- or over-reports relative
to the process's true peak footprint during the attempt, `BAND_CEILING` is
mis-derived in either direction -- admitting a rung that genuinely exhausts
memory, or declining a rung that was in fact safe. Not verified against a
live Render container at authoring time; Round 2's live ladder is the first
time this assumption is actually tested.

**Units.** Kilobytes on Linux, matching `/proc/self/status`'s own units.
`resource.getrusage` reports bytes on macOS and kilobytes on Linux -- this
module avoids that stdlib module entirely and so avoids that platform trap
outright rather than branching around it.

**Platform.** Linux only. `read_current_rss_kb()` returns `None` on every
other platform (including this project's Windows dev workstation) or on any
read/parse failure -- never raises, mirroring the fail-soft posture
`routing/services/eia.py`'s `get_factor_table()` already uses for a
completely different unreachable-dependency case. `manage.py test` therefore
stays green off Linux, and the request path (plan 26-06) must treat `None`
as "instrumentation unavailable, record nothing," never as an error.
"""
import hmac
import sys
from decimal import Decimal

# The HTTP header names carrying the probe's shared secret and the
# requested transition-budget rung, respectively. Defined here rather than
# in `config/settings/base.py` or a test module for the same one-owner
# reason the whole seam lives in this file: `routing/views.py` (plan 26-06)
# and `probe_live_latency.py` (plans 26-04/26-06) both need these exact
# strings, and production code must never import a name out of
# `routing/tests/`.
PROBE_KEY_HEADER = "X-Dispatch-Probe-Key"
PROBE_BUDGET_HEADER = "X-Dispatch-Probe-Budget"


def read_current_rss_kb():
    """Return this process's CURRENT resident set size, in kilobytes,
    parsed from `/proc/self/status`'s `VmRSS:` line -- or `None` on any
    non-Linux platform, or on any read/parse failure. Never raises. See
    this module's own docstring for why this is a delta-of-current
    reading and not a genuine peak, and for RESEARCH.md's Assumption A1
    this reading rests on.
    """
    if sys.platform != "linux":
        return None
    try:
        with open("/proc/self/status") as status_file:
            for line in status_file:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def rss_delta_mb(before_kb, after_kb):
    """Return the megabyte delta between two `read_current_rss_kb()`
    readings taken tightly around one DP attempt, as a `Decimal` -- or
    `None` if either input is `None` (instrumentation was unavailable for
    at least one of the two readings, so no delta can be trusted).

    A negative delta -- the allocator returned pages back to the OS
    mid-measurement -- is clamped to zero rather than returned as-is: a
    negative "footprint" would corrupt `derive_band_ceiling()`'s
    `peak_rss_mb * ROUTE_ALTERNATIVES_FANOUT <= budget_limit` comparison
    (`routing/tests/test_dispatch_recovery.py`), which assumes a
    non-negative footprint.
    """
    if before_kb is None or after_kb is None:
        return None
    delta_kb = after_kb - before_kb
    if delta_kb < 0:
        delta_kb = 0
    return Decimal(delta_kb) / Decimal(1024)


def resolve_probe_budget(headers, *, secret, allowed_rungs):
    """Return an `int` transition-budget rung when, and only when, ALL of:
    `secret` is a non-empty string; `headers` carries `PROBE_KEY_HEADER`;
    the two compare equal under a constant-time comparison (`hmac.compare_digest`,
    never a plain `==` -- the ASVS V6 timing-safe-comparison control
    RESEARCH.md's Security Domain section names); `headers` carries
    `PROBE_BUDGET_HEADER`; and its value parses to an `int` that is a
    member of `allowed_rungs`. Otherwise returns `None`, silently -- no
    branch anywhere in this function distinguishes "wrong secret" from
    "no secret" from "malformed budget" in its return value or in
    anything it raises, which is the inertness property T-26-08 accepts
    the residual timing signal against.

    Default posture matters most of all: an unset or empty `secret` makes
    this function return `None` for every input, including one carrying a
    correct-looking header pair. That is the property the whole probe
    seam's inertness rests on -- `settings.DISPATCH_PROBE_SECRET` is
    unset in every environment except a deliberately provisioned probe
    window (`config/settings/base.py`).
    """
    if not secret:
        return None
    provided_secret = headers.get(PROBE_KEY_HEADER)
    if not provided_secret:
        return None
    if not hmac.compare_digest(secret, provided_secret):
        return None
    provided_budget = headers.get(PROBE_BUDGET_HEADER)
    if provided_budget is None:
        return None
    try:
        rung = int(provided_budget)
    except (TypeError, ValueError):
        return None
    if rung not in allowed_rungs:
        return None
    return rung
