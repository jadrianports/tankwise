"""Unit tests for `routing.timing.ServerTiming` -- accumulation, ms
formatting, first-seen ordering, and exception-safe recording."""
import re
from unittest import mock

from django.test import SimpleTestCase

from routing.timing import ServerTiming

_METRIC_RE = re.compile(r"^[a-z]+;dur=\d+\.\d$")


class ServerTimingAccumulationTests(SimpleTestCase):
    def test_entering_same_stage_twice_accumulates_into_one_bucket(self):
        t = ServerTiming()

        with t.stage("geocode"):
            pass
        with t.stage("geocode"):
            pass

        header = t.header_value()
        self.assertEqual(header.count("geocode"), 1)


class ServerTimingFormatTests(SimpleTestCase):
    def test_header_value_matches_name_dur_shape(self):
        t = ServerTiming()

        with t.stage("route"):
            pass

        header = t.header_value()
        self.assertRegex(header, r"route;dur=\d+\.\d")


class ServerTimingOrderTests(SimpleTestCase):
    def test_first_seen_order_is_preserved(self):
        t = ServerTiming()

        with t.stage("route"):
            pass
        with t.stage("corridor"):
            pass
        with t.stage("solver"):
            pass

        header = t.header_value()
        names = [part.split(";")[0] for part in header.split(", ")]
        self.assertEqual(names, ["route", "corridor", "solver"])

    def test_all_metrics_match_expected_shape(self):
        t = ServerTiming()

        with t.stage("route"):
            pass
        with t.stage("corridor"):
            pass

        for part in t.header_value().split(", "):
            self.assertRegex(part, _METRIC_RE)


class ServerTimingExceptionSafetyTests(SimpleTestCase):
    def test_exception_inside_stage_propagates_and_is_still_recorded(self):
        t = ServerTiming()

        with self.assertRaises(ValueError):
            with t.stage("route"):
                raise ValueError("boom")

        self.assertIn("route;dur=", t.header_value())


class ServerTimingMsConversionTests(SimpleTestCase):
    def test_elapsed_time_is_converted_to_milliseconds(self):
        t = ServerTiming()

        # perf_counter() called twice per stage (__enter__, __exit__);
        # a controlled 0.05s delta must render as 50.0ms, not 0.05.
        with mock.patch("time.perf_counter", side_effect=[1.0, 1.05]):
            with t.stage("route"):
                pass

        self.assertIn("route;dur=50.0", t.header_value())
