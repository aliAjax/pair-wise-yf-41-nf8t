import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def _reports(*stations, amplitudes=(4.0, 4.2, 4.4)):
    return [
        {"station": code, "time_offset": 0, "distance_km": 1.0, "amplitudes": list(amplitudes)}
        for code in stations
    ]


class LinkageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.station = Actor("sta1", "station")
        self.reviewer = Actor("rev1", "reviewer")
        self.service.create(
            self.station, "station", {"code": "STA-1", "lat": 35.0, "lon": 110.0}
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _event(self, reports=None):
        return self.service.create(
            self.admin,
            "event",
            {
                "title": "Event-L",
                "origin_time": "2026-01-01T00:00:00Z",
                "location": "Region-L",
                "reports": reports or _reports("STA-1", "STA-2"),
            },
        )

    def _associated(self, reports=None):
        event = self._event(reports)
        self.service.transition(self.admin, event["id"], "associate")
        return self.service.get(event["id"])

    def test_concurrent_supplement_latest_wins(self):
        event = self._associated()
        version = event["version"]
        first = self.service.supplement(
            self.admin, event["id"], _reports("STA-3"), expected_version=version
        )
        self.assertEqual(first["data"]["report_version"], 2)
        with self.assertRaises(ConflictError):
            self.service.supplement(
                self.admin, event["id"], _reports("STA-4"), expected_version=version
            )
        current = self.service.get(event["id"])
        stations = {r["station"] for r in current["data"]["reports"]}
        self.assertIn("STA-3", stations)
        self.assertNotIn("STA-4", stations)
        retried = self.service.supplement(
            self.admin, event["id"], _reports("STA-4"), expected_version=version + 1
        )
        self.assertEqual(retried["data"]["report_version"], 3)
        stations = {r["station"] for r in retried["data"]["reports"]}
        self.assertIn("STA-3", stations)
        self.assertIn("STA-4", stations)

    def test_supplement_invalidates_review_and_recalculates(self):
        event = self._associated()
        self.service.transition(
            self.reviewer, event["id"], "review", {"reviewer": "rev1"}
        )
        reviewed = self.service.get(event["id"])
        self.assertEqual(reviewed["status"], "reviewed")
        self.assertEqual(reviewed["data"]["review"]["magnitude"], 4.2)
        self.assertEqual(reviewed["data"]["review"]["report_version"], 1)

        supplemented = self.service.supplement(
            self.admin, event["id"], _reports("STA-3", amplitudes=(5.0, 5.2, 5.4))
        )
        self.assertEqual(supplemented["status"], "associated")
        self.assertIsNone(supplemented["data"]["review"])
        self.assertEqual(supplemented["data"]["report_version"], 2)

        logs = self.service.audit_log(event["id"])
        actions = {entry["action"]: entry for entry in logs}
        self.assertEqual(actions["review"]["detail"]["review"]["magnitude"], 4.2)
        self.assertEqual(
            actions["supplement"]["detail"]["old_review"]["magnitude"], 4.2
        )

        self.service.transition(
            self.reviewer, event["id"], "review", {"reviewer": "rev1"}
        )
        re_reviewed = self.service.get(event["id"])
        self.assertEqual(re_reviewed["data"]["review"]["magnitude"], 4.4)
        self.assertEqual(re_reviewed["data"]["review"]["report_version"], 2)

    def test_station_cannot_supplement_other_station(self):
        event = self._associated()
        allowed = self.service.supplement(
            self.station, event["id"], _reports("STA-1")
        )
        self.assertEqual(allowed["data"]["report_version"], 2)
        with self.assertRaises(PermissionDenied):
            self.service.supplement(
                self.station, event["id"], _reports("STA-2")
            )

    def test_failed_supplement_pending_retry_no_double_count(self):
        event = self._associated()
        with self.assertRaises(ConflictError):
            self.service.supplement(
                self.admin, event["id"], _reports("STA-3"), expected_version=999
            )
        pending = self.repo.list_intake(event["id"], status="pending")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["station"], "STA-3")

        retried = self.service.supplement(
            self.admin, event["id"], _reports("STA-3"), expected_version=2
        )
        stations = [r["station"] for r in retried["data"]["reports"]]
        self.assertEqual(stations.count("STA-3"), 1)
        self.assertEqual(len(set(stations)), 3)
        pending = self.repo.list_intake(event["id"], status="pending")
        self.assertEqual(pending, [])

    def test_reconcile_mismatch_returns_for_review(self):
        event = self._associated()
        self.service.transition(
            self.reviewer, event["id"], "review", {"reviewer": "rev1"}
        )
        self.service.transition(
            self.reviewer,
            event["id"],
            "publish",
            {"communication_id": "C-1"},
        )
        published = self.service.get(event["id"])
        self.assertEqual(published["data"]["publication"]["report_version"], 1)

        matched = self.service.reconcile(self.reviewer, event["id"], "C-1")
        self.assertEqual(matched["status"], "published")

        supplemented = self.service.supplement(
            self.admin, event["id"], _reports("STA-3")
        )
        self.assertEqual(supplemented["status"], "published")
        self.assertEqual(supplemented["data"]["report_version"], 2)

        mismatched = self.service.reconcile(self.reviewer, event["id"], "C-1")
        self.assertEqual(mismatched["status"], "associated")
        self.assertIsNone(mismatched["data"]["review"])

        with self.assertRaises(ValidationError):
            self.service.reconcile(self.reviewer, event["id"], "WRONG")

    def test_review_requires_magnitude_without_amplitudes(self):
        event = self.service.create(
            self.admin,
            "event",
            {
                "title": "Event-NoAmp",
                "origin_time": "2026-01-01T00:00:00Z",
                "location": "Region-X",
                "reports": [
                    {"station": "STA-1", "time_offset": 0, "distance_km": 1.0},
                    {"station": "STA-2", "time_offset": 0, "distance_km": 1.0},
                ],
            },
        )
        self.service.transition(self.admin, event["id"], "associate")
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.reviewer, event["id"], "review", {"reviewer": "rev1"}
            )
        self.service.transition(
            self.reviewer,
            event["id"],
            "review",
            {"reviewer": "rev1", "magnitude": 3.9},
        )
        reviewed = self.service.get(event["id"])
        self.assertEqual(reviewed["data"]["review"]["magnitude"], 3.9)
        self.assertEqual(reviewed["data"]["review"]["magnitude_source"], "manual")


if __name__ == "__main__":
    unittest.main()
