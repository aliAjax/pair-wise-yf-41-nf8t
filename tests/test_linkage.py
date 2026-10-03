import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    NotFoundError,
    PermissionDenied,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class LinkageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.analyst = Actor("analyst-1", "analyst")
        self.reviewer = Actor("reviewer-1", "reviewer")
        self.event = self.service.create(
            self.admin,
            "event",
            {
                "title": "Event-L",
                "origin_time": "2026-01-01T00:00:00Z",
                "location": "Region-L",
                "reports": [
                    {"station_code": "STA-1", "amplitude": 1.0,
                     "time_offset": 1, "distance_km": 0.5},
                    {"station_code": "STA-2", "amplitude": 3.0,
                     "time_offset": 2, "distance_km": 1.0},
                ],
            },
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _station_actor(self, code, user_id=None):
        user_id = user_id or ("user-" + code)
        self.service.create(
            Actor(user_id, "station"),
            "station",
            {"code": code, "lat": 35.0, "lon": 110.0},
        )
        return Actor(user_id, "station")

    def _publish(self, communication_id="COMM-1"):
        self.service.transition(self.analyst, self.event["id"], "associate", {})
        reviewed = self.service.transition(
            self.reviewer, self.event["id"], "review",
            {"reviewer": "R-1", "magnitude": 2.0},
        )
        published = self.service.transition(
            self.reviewer, self.event["id"], "publish",
            {"communication_id": communication_id},
        )
        return reviewed, published

    # ---- 同一依据 / 版本联动 --------------------------------------------

    def test_report_entities_follow_event_report_set(self):
        reports = self.service.list("report")
        self.assertEqual(len(reports), 2)
        event = self.service.get(self.event["id"])
        self.assertEqual(event["data"]["report_set_version"], 1)
        self.assertEqual(event["data"]["station_count"], 2)
        self.assertNotIn("magnitude", event["data"])
        actor1 = self._station_actor("STA-1")
        updated = self.service.transition(
            actor1, self.event["id"], "supplement",
            {"station_code": "STA-1", "amplitude": 5.0, "ref": "r1"},
        )
        # 报告集合版本+1，事件实体版本+1，旧报告实体 superseded 留痕。
        self.assertEqual(updated["data"]["report_set_version"], 2)
        self.assertEqual(updated["version"], self.event["version"] + 1)
        reports = {r["id"]: r for r in self.service.list("report")}
        self.assertEqual(reports["STA-1@%s#v1" % self.event["id"]]["status"],
                         "superseded")
        self.assertEqual(reports["STA-1@%s#v2" % self.event["id"]]["status"],
                         "received")
        # 采用集合中每台只留最新一版：台站数不重复计入。
        self.assertEqual(updated["data"]["station_count"], 2)
        self.assertEqual(
            set(updated["data"]["reports"]),
            {"STA-1@%s#v2" % self.event["id"],
             "STA-2@%s#v1" % self.event["id"]},
        )
        self.assertEqual(updated["data"]["magnitude"], 4.0)  # median(5.0, 3.0)

    def test_concurrent_supplements_only_latest_set_accepted(self):
        actor1 = self._station_actor("STA-1")
        actor2 = self._station_actor("STA-2")
        first = self.service.transition(
            actor1, self.event["id"], "supplement",
            {"station_code": "STA-1", "amplitude": 9.0, "ref": "c1"},
        )
        # 第二个并发补报拿着旧的事件版本号：直接冲突拒绝。
        with self.assertRaises(ConflictError):
            self.service.transition(
                actor2,
                self.event["id"],
                "supplement",
                {"station_code": "STA-2", "amplitude": 9.0, "ref": "c2"},
                expected_version=self.event["version"],
            )
        # 待重试项保留；按新版本重试成功，且不重复计台站数。
        pending = self.service.list_pending(self.event["id"])
        self.assertEqual([item["ref"] for item in pending], ["c2"])
        result = self.service.transition(
            Actor("admin", "admin"), self.event["id"], "retry_pending",
            {"refs": ["c2"]},
        )
        self.assertEqual(result["applied"][0]["report_set_version"], 3)
        final = self.service.get(self.event["id"])
        self.assertEqual(final["data"]["station_count"], 2)
        self.assertEqual([item["ref"] for item in self.service.list_pending()], [])
        self.assertEqual(final["status"], "candidate")
        self.assertEqual(first["data"]["station_count"], 2)

    def test_supplement_invalidates_prior_review_and_recomputes(self):
        self._publish()
        actor1 = self._station_actor("STA-1")
        updated = self.service.transition(
            actor1, self.event["id"], "supplement",
            {"station_code": "STA-1", "amplitude": 7.0, "ref": "rev1"},
        )
        # 发布后补报：先到的复核结果立即失效，退回待复核并重算震级。
        self.assertEqual(updated["status"], "pending_review")
        self.assertNotIn("reviewer", updated["data"])
        self.assertEqual(updated["data"]["magnitude"], 5.0)  # median(7.0, 3.0)
        self.assertEqual(updated["data"]["magnitude_basis"], "auto")
        # 旧结论留痕：事件历史 + 审计时间线都可查。
        history = updated["data"]["conclusion_history"]
        self.assertEqual(history[-1]["reason"], "report_updated")
        self.assertEqual(history[-1]["magnitude"], 2.0)
        actions = [a["action"] for a in self.service.audit_log(self.event["id"])]
        self.assertIn("invalidate_review", actions)

        # 重新复核后可再次发布。
        self.service.transition(
            self.reviewer, self.event["id"], "review",
            {"reviewer": "R-2", "magnitude": 5.1},
        )
        republished = self.service.transition(
            self.reviewer, self.event["id"], "publish",
            {"communication_id": "COMM-2"},
        )
        self.assertEqual(republished["status"], "published")

    # ---- 权限 -----------------------------------------------------------

    def test_station_cannot_file_other_station_report(self):
        self._station_actor("STA-1")
        intruder = self._station_actor("STA-3", user_id="user-STA-3")
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                intruder, self.event["id"], "supplement",
                {"station_code": "STA-1", "amplitude": 1.0, "ref": "x1"},
            )
        # 越权补报直接拒绝，不留待重试。
        self.assertEqual(self.service.list_pending(self.event["id"]), [])

    def test_station_cannot_claim_taken_code(self):
        self._station_actor("STA-1")
        with self.assertRaises(PermissionDenied):
            self.service.create(
                Actor("other-station", "station"),
                "station",
                {"code": "STA-1", "lat": 1.0, "lon": 2.0},
            )

    # ---- 失败待重试 / 幂等 ---------------------------------------------

    def test_failed_write_is_retryable_without_double_counting(self):
        actor2 = self._station_actor("STA-2")
        # 制造版本冲突：补报失败后 pending 保留。
        self.service.transition(
            self._station_actor("STA-1"), self.event["id"], "supplement",
            {"station_code": "STA-1", "amplitude": 2.0, "ref": "ok1"},
        )
        with self.assertRaises(ConflictError):
            self.service.transition(
                actor2,
                self.event["id"],
                "supplement",
                {"station_code": "STA-2", "amplitude": 8.0, "ref": "fail1"},
                expected_version=1,
            )
        pending = self.service.list_pending(self.event["id"])
        self.assertEqual(len(pending), 1)
        self.assertGreaterEqual(pending[0]["attempts"], 1)
        self.assertIsNotNone(pending[0]["last_error"])

        # 台站本人重试（不带版本号，对最新集合合并）。
        result = self.service.transition(
            actor2, self.event["id"], "retry_pending", {"refs": ["fail1"]}
        )
        self.assertEqual(len(result["applied"]), 1)
        self.assertEqual(result["failures"], [])
        event = self.service.get(self.event["id"])
        self.assertEqual(event["data"]["station_count"], 2)

        # 同一 ref 再次重放：幂等返回当前事件，不新增报告、不增加台站数。
        replay = self.service.transition(
            actor2, self.event["id"], "supplement",
            {"station_code": "STA-2", "amplitude": 8.0, "ref": "fail1"},
        )
        self.assertEqual(replay["data"]["station_count"], 2)
        self.assertEqual(len(replay["data"]["reports"]), 2)
        self.assertEqual(
            replay["data"]["report_set_version"], event["data"]["report_set_version"]
        )

    def test_retry_other_actor_pending_denied(self):
        actor2 = self._station_actor("STA-2")
        with self.assertRaises(ConflictError):
            self.service.transition(
                actor2,
                self.event["id"],
                "supplement",
                {"station_code": "STA-2", "amplitude": 8.0, "ref": "own1"},
                expected_version=99,
            )
        other_station = self._station_actor("STA-3", user_id="user-STA-3")
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                other_station, self.event["id"], "retry_pending",
                {"refs": ["own1"]},
            )

    # ---- 发布对账 -------------------------------------------------------

    def test_reconcile_version_mismatch_returns_to_pending_review(self):
        self._publish("COMM-X")
        # 发布后又来补报，发布结论失效退回待复核，重新复核并发布。
        actor1 = self._station_actor("STA-1")
        self.service.transition(
            actor1, self.event["id"], "supplement",
            {"station_code": "STA-1", "amplitude": 7.0, "ref": "post1"},
        )
        self.service.transition(
            self.reviewer, self.event["id"], "review",
            {"reviewer": "R-2", "magnitude": 5.1},
        )
        self.service.transition(
            self.reviewer, self.event["id"], "publish",
            {"communication_id": "COMM-X"},
        )
        event = self.service.get(self.event["id"])
        self.assertEqual(event["status"], "published")
        current_set = event["data"]["report_set_version"]

        # 版本对不上（拿旧版本号对账）：一律退回待复核。
        mismatched = self.service.transition(
            self.reviewer, self.event["id"], "reconcile",
            {"communication_id": "COMM-X",
             "report_set_version": current_set - 1},
        )
        self.assertEqual(mismatched["status"], "pending_review")
        self.assertNotIn("reviewer", mismatched["data"])
        self.assertEqual(mismatched["data"]["conclusion_history"][-1]["reason"],
                         "reconcile_mismatch")
        audit = self.service.audit_log(self.event["id"])
        latest = [a for a in audit if a["action"] == "reconcile"][-1]
        self.assertFalse(latest["detail"]["matched"])

        # 通信编号对不上同样退回。
        self.service.transition(
            self.reviewer, self.event["id"], "review",
            {"reviewer": "R-3", "magnitude": 5.0},
        )
        self.service.transition(
            self.reviewer, self.event["id"], "publish",
            {"communication_id": "COMM-X"},
        )
        comm_mismatch = self.service.transition(
            self.reviewer, self.event["id"], "reconcile",
            {"communication_id": "WRONG",
             "report_set_version": self.service.get(self.event["id"])[
                 "data"]["report_set_version"]},
        )
        self.assertEqual(comm_mismatch["status"], "pending_review")

        # 通信编号与报告集合版本都对得上：对账通过。
        self.service.transition(
            self.reviewer, self.event["id"], "review",
            {"reviewer": "R-3", "magnitude": 5.0},
        )
        self.service.transition(
            self.reviewer, self.event["id"], "publish",
            {"communication_id": "COMM-X"},
        )
        matched = self.service.transition(
            self.reviewer, self.event["id"], "reconcile",
            {"communication_id": "COMM-X",
             "report_set_version": self.service.get(self.event["id"])[
                 "data"]["report_set_version"]},
        )
        self.assertEqual(matched["status"], "reconciled")

    def test_duplicate_communication_id_rejected(self):
        self._publish("COMM-DUP")
        other = self.service.create(
            self.admin,
            "event",
            {
                "title": "Event-M",
                "origin_time": "2026-01-02T00:00:00Z",
                "location": "Region-M",
                "reports": [
                    {"station_code": "STA-1", "amplitude": 1.0},
                    {"station_code": "STA-2", "amplitude": 2.0},
                ],
            },
        )
        self.service.transition(self.analyst, other["id"], "associate", {})
        self.service.transition(
            self.reviewer, other["id"], "review",
            {"reviewer": "R-1", "magnitude": 1.5},
        )
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.reviewer, other["id"], "publish",
                {"communication_id": "COMM-DUP"},
            )


if __name__ == "__main__":
    unittest.main()
