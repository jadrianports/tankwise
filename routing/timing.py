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
