import unittest

from src.rules import associate_reports, magnitude_median
from src.domain import Actor, PermissionDenied, ValidationError
from src.rules import RuleEngine


class RulesTest(unittest.TestCase):
    def setUp(self):
        self.rules = RuleEngine()
        self.admin = Actor("rule-tester", "admin")

    def test_rule_calculation_or_validation(self):
        reports = [
            {"station": "A", "time_offset": 1, "distance_km": 0.5},
            {"station": "B", "time_offset": 2, "distance_km": 1.0},
        ]
        self.assertEqual(len(associate_reports(reports)), 2)
        self.assertEqual(magnitude_median([1.0, 4.0, 2.0]), 2.0)


if __name__ == "__main__":
    unittest.main()
