import ast
import pathlib

from django.test import SimpleTestCase

ROUTING_DIR = pathlib.Path(__file__).resolve().parent.parent
SERVICES_DIR = ROUTING_DIR / "services"
FORBIDDEN_PREFIX = "routing.pipeline"

# `naive_baseline.solve()` is a completely different function (the
# deliberately price-blind baseline solver, unchanged this phase per
# STATE.md's v3.1 decision log) that has no `penalty` parameter at all and
# must never be flagged by `SolvePenaltyKwargGateTest` below; only the
# fixed-charge solver's own `solve()` (called as bare `solve(...)` or
# `solver.solve(...)`) is in scope for that gate.
_NON_TARGET_SOLVE_BASES = {"naive_baseline"}

SOLVER_FILES = [
    SERVICES_DIR / "solver.py",
    SERVICES_DIR / "exceptions.py",
    SERVICES_DIR / "prune.py",
    SERVICES_DIR / "dp.py",
    SERVICES_DIR / "greedy.py",
    SERVICES_DIR / "heuristic.py",
]
SOLVER_FORBIDDEN_PREFIXES = (
    "django",
    "routing.models",
    "routing.pipeline",
    "requests",
    "httpx",
    "urllib.request",
    "http.client",
)


def _collect_import_names(path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    return names


class ImportBoundaryTest(SimpleTestCase):
    """Statically enforces that routing/services/ (the request-path layer)
    never imports routing/pipeline/ (the offline-only geocoding layer).
    Vacuously true while services/ is empty; load-bearing the moment a
    later addition introduces a bad import.
    """

    def test_services_never_import_pipeline(self):
        violations = []
        for path in SERVICES_DIR.rglob("*.py"):
            for name in _collect_import_names(path):
                if name.startswith(FORBIDDEN_PREFIX):
                    violations.append(f"{path}: imports {name}")

        self.assertEqual(
            violations,
            [],
            f"routing/services/ must never import routing/pipeline/: {violations}",
        )

    def test_mapbox_and_corridor_modules_are_scanned_and_pipeline_free(self):
        """Regression test: mapbox.py and corridor.py legitimately
        import django/routing.models/requests (deliberately NOT added to
        SOLVER_FILES below -- they would trip SolverPurityTest's stricter
        gate), but the broader ImportBoundaryTest scan above must still
        cover them and confirm neither imports the offline geocoding
        pipeline package.
        """
        scanned = set(SERVICES_DIR.rglob("*.py"))
        mapbox_path = SERVICES_DIR / "mapbox.py"
        corridor_path = SERVICES_DIR / "corridor.py"

        self.assertIn(mapbox_path, scanned)
        self.assertIn(corridor_path, scanned)

        for path in (mapbox_path, corridor_path):
            violations = [
                name
                for name in _collect_import_names(path)
                if name.startswith(FORBIDDEN_PREFIX)
            ]
            self.assertEqual(violations, [], f"{path}: imports {violations}")


class SolverPurityTest(SimpleTestCase):
    """Statically enforces that the solver (routing/services/solver.py,
    routing/services/exceptions.py, routing/services/prune.py,
    routing/services/dp.py, and routing/services/greedy.py -- the
    fixed-charge DP's production fallback, added Phase 18-04c) must stay
    free of Django, the ORM, the offline geocoding pipeline, and any HTTP
    client. Scoped to just these five files -- not all of services/ -- so
    a later Station -> Candidate adapter is free to import routing.models
    elsewhere in services/ without tripping this gate.
    """

    def test_solver_files_never_import_django_orm_pipeline_or_http(self):
        violations = []
        for path in SOLVER_FILES:
            for name in _collect_import_names(path):
                if any(name.startswith(prefix) for prefix in SOLVER_FORBIDDEN_PREFIXES):
                    violations.append(f"{path}: imports {name}")

        self.assertEqual(
            violations,
            [],
            f"solver.py/exceptions.py/prune.py/dp.py must never import django/ORM/pipeline/HTTP: {violations}",
        )


def _collect_solve_calls_missing_kwarg(path, kwarg_name):
    """Find every fixed-charge `solve(...)` call site in `path` missing an
    explicit `{kwarg_name}=` keyword. Treats a node as a `solve()` call
    when `func` is a bare `ast.Name` with `id == "solve"`, or an
    `ast.Attribute` with `attr == "solve"` whose base object name is not
    in `_NON_TARGET_SOLVE_BASES` -- `naive_baseline.solve()` has no
    `penalty`/`trust_margin` parameter at all (it is a different function
    entirely, the deliberately price-blind baseline) and must never be
    flagged here.

    Generalized from the plan's own two faithful options (clone the
    walker, or parameterize it and run it twice) -- this file picks the
    latter: `_collect_solve_calls_missing_penalty` and
    `_collect_solve_calls_missing_trust_margin` below are both thin,
    single-line wrappers over this one walker, so the AST-matching logic
    (which node counts as a target `solve()` call) is defined exactly
    once rather than duplicated and risking the two copies drifting apart
    on what counts as a target call.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_target = False
        if isinstance(func, ast.Name) and func.id == "solve":
            is_target = True
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == "solve"
            and not (
                isinstance(func.value, ast.Name)
                and func.value.id in _NON_TARGET_SOLVE_BASES
            )
        ):
            is_target = True
        if not is_target:
            continue
        has_kwarg = any(kw.arg == kwarg_name for kw in node.keywords)
        if not has_kwarg:
            violations.append(
                f"{path}:{node.lineno}: solve() call missing {kwarg_name}="
            )
    return violations


def _collect_solve_calls_missing_penalty(path):
    """`penalty=` specialization of `_collect_solve_calls_missing_kwarg`
    -- see that function's own docstring."""
    return _collect_solve_calls_missing_kwarg(path, "penalty")


def _collect_solve_calls_missing_trust_margin(path):
    """`trust_margin=` specialization of
    `_collect_solve_calls_missing_kwarg` (PROV-03, D-16) -- see that
    function's own docstring."""
    return _collect_solve_calls_missing_kwarg(path, "trust_margin")


_PRICE_SOURCE_DECISION_TYPES = (ast.Compare, ast.BoolOp, ast.IfExp)


def _build_parent_index(tree):
    """Map `id(child) -> (parent_node, field_name)` for every node in
    `tree`. The stdlib gives no parent pointer, so this is built by hand
    once per parse via `ast.iter_fields`, the same idiom the file's other
    two walkers already use for `ast.walk`.
    """
    index = {}
    for node in ast.walk(tree):
        for field_name, value in ast.iter_fields(node):
            if isinstance(value, ast.AST):
                index[id(value)] = (node, field_name)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        index[id(item)] = (node, field_name)
    return index


def _price_source_reference_name(node):
    """Return the matched identifier if `node` is a reference to
    something whose name mentions `price_source` -- an `ast.Attribute`
    whose `attr` contains the substring, an `ast.Name` whose `id`
    contains it (this is what catches a walk-local tracking variable such
    as `heuristic.py`'s `current_price_source`), or the `arg` of an
    `ast.keyword`. Substring matching, not exact equality, is deliberate:
    the field is threaded through prefixed locals. Returns `None` when
    `node` is not such a reference.
    """
    if isinstance(node, ast.Attribute) and "price_source" in node.attr:
        return node.attr
    if isinstance(node, ast.Name) and "price_source" in node.id:
        return node.id
    if isinstance(node, ast.keyword) and node.arg and "price_source" in node.arg:
        return node.arg
    return None


def _is_price_source_decision_position(node, parent_index):
    """Walk `node`'s ancestor chain upward until the nearest enclosing
    `ast.stmt`, and decide whether any edge on that chain places the
    reference in a comparison, conditional, boolean, or sort-key
    position rather than a plain assignment/construction position.

    Flagged positions: an `ast.Compare`, `ast.BoolOp`, or `ast.IfExp`
    ancestor; a `lambda` (`ast.Lambda`) whose own parent is an
    `ast.keyword` named `key`; the `test` field of an `ast.If`/
    `ast.While`; the `ifs` field of an `ast.comprehension`; the `value`
    of an `ast.keyword` named `key` on any `ast.Call`; an `ast.Assert`;
    or an `ast.Match` subject.

    Everything else -- a keyword argument name or value in a
    constructor/function call, the target or value of an
    `ast.Assign`/`ast.AnnAssign`, an annotation, a dataclass field
    declaration, a returned attribute access, or an element of a plain
    tuple/list/dict literal -- is left unflagged by simply never
    matching one of the rules above.
    """
    current = node
    while id(current) in parent_index:
        parent, field_name = parent_index[id(current)]

        if isinstance(parent, _PRICE_SOURCE_DECISION_TYPES):
            return True
        if isinstance(parent, ast.Lambda):
            grandparent_info = parent_index.get(id(parent))
            if grandparent_info is not None:
                grandparent, _ = grandparent_info
                if isinstance(grandparent, ast.keyword) and grandparent.arg == "key":
                    return True
        if isinstance(parent, (ast.If, ast.While)) and field_name == "test":
            return True
        if isinstance(parent, ast.comprehension) and field_name == "ifs":
            return True
        if isinstance(parent, ast.keyword) and parent.arg == "key":
            return True
        if isinstance(parent, ast.Assert):
            return True
        if isinstance(parent, ast.Match) and field_name == "subject":
            return True

        if isinstance(parent, ast.stmt):
            break
        current = parent
    return False


def _enclosing_function_name(node, parent_index):
    """Walk `node`'s ancestor chain, via `parent_index`, up to the module
    root, and return the `name` of the nearest enclosing
    `ast.FunctionDef`/`ast.AsyncFunctionDef`. A reference with no
    enclosing function at all -- sitting at plain module scope -- is
    attributed to the sentinel `"<module>"` rather than silently
    skipped, so a module-scope provenance decision (exactly the shape
    this guard exists to catch) cannot evade attribution and therefore
    cannot evade the allowlist below.
    """
    current = node
    while id(current) in parent_index:
        parent, _field_name = parent_index[id(current)]
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return parent.name
        current = parent
    return "<module>"


def _collect_price_source_decision_reads(path):
    """Find every reference to `price_source` (or a name mentioning it)
    in `path` that sits in a comparison, conditional, boolean, or
    sort-key decision position. Returns a list of
    `(file_stem, enclosing_function_name, message)` tuples -- the first
    two fields are the `(file stem, enclosing function name)` key
    `PriceSourceUsagePurityTest`'s allowlist is keyed by; the third is a
    human-readable `"{path}:{lineno}: ..."` string, the same shape
    `_collect_solve_calls_missing_penalty` already uses for its own
    violation messages.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    parent_index = _build_parent_index(tree)

    violations = []
    for node in ast.walk(tree):
        identifier = _price_source_reference_name(node)
        if identifier is None:
            continue
        if _is_price_source_decision_position(node, parent_index):
            function_name = _enclosing_function_name(node, parent_index)
            violations.append(
                (
                    path.stem,
                    function_name,
                    f"{path}:{node.lineno}: price_source ({identifier}) read in "
                    f"a comparison/conditional/boolean/sort-key position "
                    f"(function={function_name!r})",
                )
            )
    return violations


# D-23: the Phase 21 (PROV-03) inversion target. Exactly one entry --
# `solver.py` / `is_estimate_priced` -- the trust margin's single shared
# decision-position provenance read (see that function's own docstring in
# `routing/services/solver.py`). A future phase adding a second
# decision-position provenance read anywhere in `SOLVER_FILES` must extend
# this allowlist consciously, in the same commit as the read; a stale entry
# (naming a function that no longer contains such a read) must also fail,
# which is why the guard below uses exact set equality rather than a subset
# check -- a subset check would let this allowlist silently outgrow the
# code, the same slack that makes `prune(x) -> x` pass every soundness
# property.
_PRICE_SOURCE_DECISION_READ_ALLOWLIST = frozenset(
    {
        ("solver", "is_estimate_priced"),
    }
)


class PriceSourceUsagePurityTest(SimpleTestCase):
    """Statically enforces that `price_source` is read in a decision
    position (a comparison, a conditional, a boolean operand, or a sort
    key) inside the pure solver files (`SOLVER_FILES` above) at ONLY the
    exact `(file stem, enclosing function name)` pairs pinned in
    `_PRICE_SOURCE_DECISION_READ_ALLOWLIST` -- never zero, never more, and
    never a different one.

    History: Phase 20 shipped `price_source` threaded through the solver
    but functionally inert, and this guard originally forbade EVERY
    decision-position read outright (`assertEqual(violations, [])`) to
    convert that inertness from an observation about one measured run
    into a property of the source itself. Phase 21 (PROV-03, the trust
    margin) deliberately makes exactly one such read -- pricing an
    estimate-priced candidate's provenance into the DP's internal
    objective -- so the guard is narrowed here to an allowlist of that one
    read, per this project's standing rule that guards get inverted, never
    deleted, when the behaviour they forbade becomes deliberately
    sanctioned. `assertEqual` on sorted collections (not a subset check)
    is what makes this an inversion rather than a widening: it fails in
    BOTH directions -- an unexpected new decision-position read anywhere
    in `SOLVER_FILES` (including a second one inside `solver.py` itself),
    and a stale allowlist entry naming a function that no longer contains
    such a read. A future phase introducing a genuinely new
    decision-position provenance read must extend this allowlist
    consciously, in the same commit as the read that requires it.
    """

    def test_price_source_decision_reads_match_the_pinned_allowlist_exactly(self):
        found_keys = set()
        messages_by_key = {}
        for path in SOLVER_FILES:
            for file_stem, function_name, message in _collect_price_source_decision_reads(path):
                key = (file_stem, function_name)
                found_keys.add(key)
                messages_by_key.setdefault(key, []).append(message)

        self.assertEqual(
            sorted(found_keys),
            sorted(_PRICE_SOURCE_DECISION_READ_ALLOWLIST),
            "Decision-position price_source reads in SOLVER_FILES must match "
            "the pinned allowlist exactly (D-23) -- an unexpected new read is "
            "as much a failure here as a stale allowlist entry. "
            f"found={sorted(found_keys)}; "
            f"allowlist={sorted(_PRICE_SOURCE_DECISION_READ_ALLOWLIST)}; "
            f"messages={messages_by_key}",
        )


class SolvePenaltyKwargGateTest(SimpleTestCase):
    """Statically enforces that every production call to the fixed-charge
    solver's `solve()` (`routing/services/solver.py`) passes an explicit
    `penalty=` keyword. A future production code path calling `solve()`
    without an explicit `penalty=` would silently default to `Decimal(0)`
    and revert to pre-v3.1 behaviour undetected -- this gate recovers the
    only property a required keyword-only parameter would have bought,
    without forcing 27 mechanical edits to `routing/tests/test_solver.py`
    (which deliberately omits `penalty=` on every call to exercise the
    documented penalty=0 default, greedy-identical behaviour).

    Scoped to every `.py` file under `routing/` excluding `routing/tests/`
    -- a fixed `SOLVER_FILES`-style list would need updating every time a
    new caller file appears, defeating the gate's purpose.
    """

    def test_every_production_solve_call_passes_penalty(self):
        violations = []
        for path in ROUTING_DIR.rglob("*.py"):
            if "tests" in path.relative_to(ROUTING_DIR).parts:
                continue
            violations.extend(_collect_solve_calls_missing_penalty(path))

        self.assertEqual(
            violations, [], f"solve() call(s) missing penalty=: {violations}"
        )


# D-18: every production `solve()` call site, classified by `ast`. Recorded
# here (and in 21-06-SUMMARY.md in full) rather than left as a plain count
# so a future reader can see the total/production/test split at a glance
# without re-running the walk -- 18.1-05 did the identical thing for the
# `deadline=` kwarg and found 57 sites; this headline count is that same
# discipline applied to `trust_margin=`.
TRUST_MARGIN_CALL_SITE_TOTAL_COUNT = 79
TRUST_MARGIN_CALL_SITE_PRODUCTION_COUNT = 9
TRUST_MARGIN_CALL_SITE_TEST_COUNT = 70


class SolveTrustMarginKwargGateTest(SimpleTestCase):
    """Statically enforces that every production call to the fixed-charge
    solver's `solve()` (`routing/services/solver.py`) passes an explicit
    `trust_margin=` keyword -- PROV-03, D-16, cloned from
    `SolvePenaltyKwargGateTest` above via the shared
    `_collect_solve_calls_missing_kwarg` walker (see that function's own
    docstring for why a shared walker was chosen over a hand-duplicated
    second copy). A future production code path calling `solve()` without
    an explicit `trust_margin=` would silently default to `Decimal(0)` and
    never price an `eia_regional_estimate`-priced candidate's provenance
    into the objective, undetected -- this gate recovers the only property
    a required keyword-only parameter would have bought, without forcing
    mechanical edits to every test module that deliberately omits
    `trust_margin=` to exercise the documented `trust_margin=0` default
    (byte-identical-to-pre-margin behaviour).

    Scoped identically to `SolvePenaltyKwargGateTest`: every `.py` file
    under `routing/` excluding `routing/tests/`, with the same
    `_NON_TARGET_SOLVE_BASES` exclusion for `naive_baseline.solve()` (a
    different function entirely, with no `trust_margin` parameter).

    `TRUST_MARGIN_CALL_SITE_TOTAL_COUNT` /
    `..._PRODUCTION_COUNT` / `..._TEST_COUNT` above are the D-18
    call-site classification headline counts, walked once by
    `test_call_site_classification_matches_pinned_counts` below and cross-
    checked to sum correctly -- the same non-vacuity discipline every
    other pinned-count guard in this codebase uses.
    """

    def test_every_production_solve_call_passes_trust_margin(self):
        violations = []
        for path in ROUTING_DIR.rglob("*.py"):
            if "tests" in path.relative_to(ROUTING_DIR).parts:
                continue
            violations.extend(_collect_solve_calls_missing_trust_margin(path))

        self.assertEqual(
            violations, [], f"solve() call(s) missing trust_margin=: {violations}"
        )

    def test_call_site_classification_matches_pinned_counts(self):
        production_count = 0
        test_count = 0
        for path in ROUTING_DIR.rglob("*.py"):
            is_test_path = "tests" in path.relative_to(ROUTING_DIR).parts
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                is_target = False
                if isinstance(func, ast.Name) and func.id == "solve":
                    is_target = True
                elif (
                    isinstance(func, ast.Attribute)
                    and func.attr == "solve"
                    and not (
                        isinstance(func.value, ast.Name)
                        and func.value.id in _NON_TARGET_SOLVE_BASES
                    )
                ):
                    is_target = True
                if not is_target:
                    continue
                if is_test_path:
                    test_count += 1
                else:
                    production_count += 1

        self.assertEqual(
            production_count,
            TRUST_MARGIN_CALL_SITE_PRODUCTION_COUNT,
            "production solve() call-site count drifted from the pinned "
            f"D-18 classification: found {production_count}, "
            f"pinned {TRUST_MARGIN_CALL_SITE_PRODUCTION_COUNT}",
        )
        self.assertEqual(
            test_count,
            TRUST_MARGIN_CALL_SITE_TEST_COUNT,
            "test solve() call-site count drifted from the pinned D-18 "
            f"classification: found {test_count}, "
            f"pinned {TRUST_MARGIN_CALL_SITE_TEST_COUNT}",
        )
        self.assertEqual(
            production_count + test_count,
            TRUST_MARGIN_CALL_SITE_TOTAL_COUNT,
            "production + test solve() call-site counts do not sum to the "
            "pinned D-18 total",
        )
