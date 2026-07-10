from decimal import Decimal

from django.test import SimpleTestCase

from routing.pipeline.dedupe import collapse_duplicates


def make_row(opis_id, name, address, city, state, rack_id, price):
    return {
        "OPIS Truckstop ID": str(opis_id),
        "Truckstop Name": name,
        "Address": address,
        "City": city,
        "State": state,
        "Rack ID": rack_id,
        "Retail Price": price,
    }


class CollapseDuplicatesPriceTests(SimpleTestCase):
    def test_lower_median_of_four_observations_is_an_observed_value(self):
        rows = [
            make_row(20, "PILOT #1243", "I-8, EXIT 119 & SR-85", "Gila Bend", "AZ", "930", "3.259"),
            make_row(20, "PILOT #1243", "I-8, EXIT 119 & SR-85", "Gila Bend", "AZ", "930", "3.299"),
            make_row(20, "PILOT #1243", "I-8, EXIT 119 & SR-85", "Gila Bend", "AZ", "930", "3.199"),
            make_row(20, "PILOT #1243", "I-8, EXIT 119 & SR-85", "Gila Bend", "AZ", "930", "3.359"),
        ]

        groups, report = collapse_duplicates(rows)

        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group.retail_price, Decimal("3.259"))
        self.assertNotEqual(group.retail_price, Decimal("3.279"))


class CollapseDuplicatesNameTests(SimpleTestCase):
    def test_longest_name_variant_wins(self):
        rows = [
            make_row(20, "PILOT #1243", "I-8, EXIT 119 & SR-85", "Gila Bend", "AZ", "930", "3.899"),
            make_row(20, "PILOT TRAVEL CENTER #1243", "I-8, EXIT 119 & SR-85", "Gila Bend", "AZ", "930", "3.899"),
        ]

        groups, report = collapse_duplicates(rows)

        self.assertEqual(groups[0].name, "PILOT TRAVEL CENTER #1243")

    def test_equal_length_names_first_occurrence_wins(self):
        rows = [
            make_row(30, "AAAA STOP", "100 Main St", "Anytown", "TX", "100", "3.100"),
            make_row(30, "ZZZZ STOP", "100 Main St", "Anytown", "TX", "100", "3.100"),
        ]

        groups, report = collapse_duplicates(rows)

        self.assertEqual(groups[0].name, "AAAA STOP")


class CollapseDuplicatesProvenanceTests(SimpleTestCase):
    def test_provenance_counts_and_bounds(self):
        rows = [
            make_row(40, "STOP A", "1 Rd", "Town", "TX", "100", "3.100"),
            make_row(40, "STOP A", "1 Rd", "Town", "TX", "100", "3.500"),
            make_row(40, "STOP A", "1 Rd", "Town", "TX", "100", "3.300"),
        ]

        groups, report = collapse_duplicates(rows)
        group = groups[0]

        self.assertEqual(group.observation_count, 3)
        self.assertEqual(group.price_min, Decimal("3.100"))
        self.assertEqual(group.price_max, Decimal("3.500"))


class CollapseDuplicatesReportTests(SimpleTestCase):
    def test_report_splits_exact_duplicate_from_conflicting_price_groups(self):
        rows = [
            # Exact-duplicate group: identical price across observations.
            make_row(50, "STOP A", "1 Rd", "Town", "TX", "100", "3.100"),
            make_row(50, "STOP A", "1 Rd", "Town", "TX", "100", "3.100"),
            # Conflicting-price group: prices differ across observations.
            make_row(60, "STOP B", "2 Rd", "Village", "TX", "200", "3.100"),
            make_row(60, "STOP B", "2 Rd", "Village", "TX", "200", "3.500"),
            # Single-observation group: not a duplicate at all.
            make_row(70, "STOP C", "3 Rd", "City", "TX", "300", "3.700"),
        ]

        groups, report = collapse_duplicates(rows)

        self.assertEqual(len(groups), 3)
        self.assertEqual(report.exact_duplicate_group_count, 1)
        self.assertEqual(report.conflicting_price_group_count, 1)


class CollapseDuplicatesCountryClassificationTests(SimpleTestCase):
    def test_lower_48_state_is_in_scope_and_canadian_province_is_out_of_scope(self):
        rows = [
            make_row(80, "US STOP", "1 Rd", "Town", "TX", "100", "3.100"),
            make_row(90, "CA STOP", "2 Rd", "Town", "ON", "200", "3.200"),
        ]

        groups, report = collapse_duplicates(rows)
        by_id = {g.opis_id: g for g in groups}

        self.assertFalse(by_id[80].out_of_scope)
        self.assertTrue(by_id[90].out_of_scope)
