"""Per-request Server-Timing collector.

`ServerTiming` accumulates named-stage durations for a single request. A
stage entered more than once (e.g. "geocode" for both start and finish
endpoints) accumulates into a single running total rather than overwriting
or duplicating. `_Stage.__exit__` always records elapsed time -- on success
AND on exception -- and never suppresses the exception (returns `False`),
so wrapping a pipeline call in `with timer.stage(...)` adds no try/except
to the caller's control flow.
"""
import time


class ServerTiming:
    def __init__(self):
        self._durations_ms = {}  # name -> accumulated float ms
        self._order = []  # first-seen order, for stable header output

    def stage(self, name):
        return _Stage(self, name)

    def record(self, name, elapsed_ms):
        """Record a duration that was already measured elsewhere, rather
        than timed by wrapping a block of code in `stage()`.

        Some signals are a duration a caller measured itself -- e.g. a DP
        deadline breach, measured inside the pure solver
        (`routing.services.dp`), which has no `ServerTiming` object and
        must never be given one (the solver stays free of any timing side
        channel; see `routing.services.dp`/`solver.py`'s purity docstrings
        and `SolverPurityTest`). `record()` is the public entry point for
        exactly that case: the caller reads its own already-computed
        elapsed figure and hands it in directly.

        Accumulation semantics are unchanged from `stage()` -- calling
        `record()` more than once under the same `name` in one request
        sums into a single running total, not the last call's value alone.
        This is the desired behaviour when more than one route alternative
        breaches within the same request: the header reports the total
        breach time across all of them.
        """
        self._record(name, elapsed_ms)

    def _record(self, name, elapsed_ms):
        if name not in self._durations_ms:
            self._durations_ms[name] = 0.0
            self._order.append(name)
        self._durations_ms[name] += elapsed_ms

    def header_value(self):
        parts = [f"{name};dur={self._durations_ms[name]:.1f}" for name in self._order]
        return ", ".join(parts)


class _Stage:
    def __init__(self, timer, name):
        self._timer = timer
        self._name = name

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb):
        elapsed_ms = (time.perf_counter() - self._start) * 1000
        self._timer._record(self._name, elapsed_ms)
        return False  # never suppress -- exception (if any) propagates untouched
