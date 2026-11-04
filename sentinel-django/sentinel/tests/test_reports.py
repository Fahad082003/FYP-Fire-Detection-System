"""Reports page: incident totals, breakdowns, false-alarm rate and the daily trend for a range of UTC days."""
import json
import shutil
import tempfile
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.utils import timezone

from sentinel import services
from sentinel.models import IncidentAlert, IncidentLog

from .base import SentinelTestCase


class ReportTests(SentinelTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.operator)
        self.today = timezone.now().date()

    def at(self, day, hour=12, minute=0, second=0, micro=0):
        return datetime.combine(day, time(hour, minute, second, micro), tzinfo=dt_timezone.utc)

    def incident(self, when, severity="Medium", source="Manual", zone="Ground Floor - Lobby"):
        return IncidentAlert.objects.create(timestamp=when, updated_at=when, source=source, zone=zone, severity=severity)

    def report(self, **params):
        r = self.client.get("/reports", params)
        self.assertEqual(r.status_code, 200, r.content[:300])
        return r, r.context["report"]

    def counts(self, rows):
        return {row["name"]: row["count"] for row in rows}

    def test_totals_statuses_breakdowns_and_false_alarm_rate(self):
        day = self.today - timedelta(days=3)
        self.incident(self.at(day), "High", "Vision")  # stays open
        self.incident(self.at(day, 1), "Low", "SensorLost")  # stays open
        resolved = self.incident(self.at(day, 2), "Critical", "Sensor")
        false_1 = self.incident(self.at(day, 3), "Medium", "Manual")
        false_2 = self.incident(self.at(day, 4), "Low", "CameraLost")
        self.assertEqual(self.op.post(f"/api/incidents/{resolved.id}/resolve").status_code, 200)
        for i in (false_1, false_2, false_2):  # a second click on the same incident is no second false alarm
            self.assertEqual(self.op.post(f"/api/incidents/{i.id}/false-alarm").status_code, 200)

        page, r = self.report(start=str(day), end=str(day))
        self.assertEqual((r["total"], r["open"], r["resolved"], r["falseAlarms"]), (5, 2, 3, 2))
        self.assertAlmostEqual(r["falseAlarmRate"], 2 / 3)  # false alarms / resolved incidents
        self.assertEqual(r["bySeverity"], [{"name": "Low", "count": 2}, {"name": "Medium", "count": 1},
                                           {"name": "High", "count": 1}, {"name": "Critical", "count": 1}])
        self.assertEqual(self.counts(r["bySource"]),
                         {"Vision": 1, "Sensor": 1, "Manual": 1, "CameraLost": 1, "SensorLost": 1, "Crowd": 0})
        self.assertEqual(r["daily"], [{"date": day, "count": 5}])
        self.assertContains(page, "66.7% of resolved")
        # nothing is deleted: false alarms stay resolved incidents in the history
        self.assertEqual({i["id"] for i in self.incidents(resolved="true")}, {resolved.id, false_1.id, false_2.id})
        self.assertEqual(IncidentLog.objects.filter(type="FalseAlarm").count(), 2)

    def test_dates_are_utc_days_with_both_ends_included(self):
        first, last = self.today - timedelta(days=12), self.today - timedelta(days=10)
        self.incident(self.at(first - timedelta(days=1), 23, 59, 59, 999999))  # the evening before: out
        inside = [self.incident(self.at(first, 0)), self.incident(self.at(last, 23, 59, 59, 999999))]
        self.incident(self.at(last + timedelta(days=1), 0))  # midnight after the end day: out
        _, r = self.report(start=str(first), end=str(last))
        self.assertEqual(r["total"], len(inside))
        self.assertEqual([(d["date"], d["count"]) for d in r["daily"]],
                         [(first, 1), (first + timedelta(days=1), 0), (last, 1)])  # a quiet day is shown as 0
        _, one_day = self.report(start=str(last), end=str(last))
        self.assertEqual(one_day["total"], 1)

    def test_the_default_period_is_the_last_7_days_including_today(self):
        self.incident(timezone.now())
        self.incident(self.at(self.today - timedelta(days=6), 0))
        self.incident(self.at(self.today - timedelta(days=7), 23, 59))  # 8th day back: out
        page, r = self.report()
        self.assertEqual((r["start"], r["end"]), (self.today - timedelta(days=6), self.today))
        self.assertEqual((r["total"], len(r["daily"])), (2, 7))
        self.assertContains(page, f'value="{self.today - timedelta(days=6)}"')
        _, only_start = self.report(start=str(self.today - timedelta(days=1)))  # a missing end keeps its default (today)
        self.assertEqual((only_start["end"], only_start["total"]), (self.today, 1))

    def test_no_incidents_and_no_resolved_incidents_give_no_rate_instead_of_dividing_by_zero(self):
        page, r = self.report()
        self.assertEqual((r["total"], r["open"], r["resolved"], r["falseAlarms"], r["falseAlarmRate"]), (0, 0, 0, 0, None))
        self.assertEqual(set(self.counts(r["bySeverity"]).values()) | set(self.counts(r["bySource"]).values()), {0})
        self.assertContains(page, "No incidents were opened in this period.")
        self.assertContains(page, "no rate: nothing resolved")
        self.assertNotContains(page, "Incidents per day")
        self.incident(timezone.now())  # one open incident: still nothing resolved
        page, r = self.report()
        self.assertEqual((r["total"], r["falseAlarmRate"]), (1, None))
        self.assertContains(page, "Incidents per day")

    def test_invalid_or_too_broad_ranges_are_refused(self):
        today = self.today
        cases = {
            (str(today), str(today - timedelta(days=1))): "Start date must be on or before the end date.",
            (str(today - timedelta(days=366)), str(today)): "Choose at most 366 days.",  # 367 days
            ("2026-13-01", str(today)): "Start: use a date like 2026-10-01.",
            (str(today), "10/31/2026"): "End: use a date like 2026-10-31.",
            ("", str(today)): "Start date is required.",
            (str(today), str(today + timedelta(days=2))): "End date cannot be in the future.",
            ("9999-12-31", "9999-12-31"): "End date cannot be in the future.",  # would overflow the day after
        }
        for (start, end), message in cases.items():
            with self.subTest(start=start, end=end):
                r = self.client.get("/reports", {"start": start, "end": end})
                self.assertContains(r, message, status_code=400)
                self.assertIsNone(r.context["report"])
                self.assertNotContains(r, 'id="report-kpis"', status_code=400)
        _, r = self.report(start=str(today - timedelta(days=365)), end=str(today))  # exactly 366 days
        self.assertEqual(len(r["daily"]), 366)
        _, r = self.report(start=str(today), end=str(today + timedelta(days=1)))  # local "today" east of UTC can be tomorrow in UTC
        self.assertEqual(len(r["daily"]), 2)

    def test_signed_out_users_are_sent_to_login_and_reading_a_report_changes_nothing(self):
        self.incident(timezone.now())
        before = (IncidentAlert.objects.count(), IncidentLog.objects.count(), IncidentAlert.objects.filter(is_resolved=True).count())
        self.report()
        self.assertEqual(before, (IncidentAlert.objects.count(), IncidentLog.objects.count(),
                                  IncidentAlert.objects.filter(is_resolved=True).count()))
        self.client.logout()
        r = self.client.get("/reports")
        self.assertEqual((r.status_code, r["Location"]), (302, "/login?next=/reports"))


class EvaluationReportTests(SentinelTestCase):
    """The Reports page's model accuracy: offline runs of ai-service/tools/evaluate.py against labelled images."""
    RUN = {"evaluatedAt": "2026-11-01T10:00:00+00:00", "dataset": "labelled test split", "slice": None, "images": 10, "iouThreshold": 0.5,
           "models": {"fire_smoke": "fire_smoke_yolox.onnx"}, "configuredThresholds": {"fire_smoke": 0.4},
           "averagePrecision": {"Fire": {"AP50": 0.7}},
           "byThreshold": {"0.25": {"Fire": {"tp": 9, "fp": 9, "fn": 0, "precision": 0.5, "recall": 1.0, "f1": 0.667}},
                           "0.4": {"Fire": {"tp": 6, "fp": 2, "fn": 3, "precision": 0.75, "recall": 0.6667, "f1": 0.7059,
                                            "imageLevel": {"tp": 4, "fp": 1, "fn": 1, "tn": 4, "falsePositiveRate": 0.2}},
                                   "Smoke": {"tp": 0, "fp": 0, "fn": 5, "precision": None, "recall": 0.0, "f1": None,
                                             "imageLevel": {"tp": 0, "fp": 0, "fn": 5, "tn": 0, "falsePositiveRate": None}}}}}

    def setUp(self):
        super().setUp()
        self.client.force_login(self.operator)
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        patcher = mock.patch.dict(settings.SENTINEL, AI_EVALUATION_DIR=self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, folder, data):
        (self.dir / folder).mkdir(parents=True)
        (self.dir / folder / "report.json").write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")

    def test_metrics_are_the_runs_own_numbers_at_the_configured_threshold(self):
        self.write("2026-11-01-fire", self.RUN)
        [run] = services.evaluations()
        self.assertEqual((run["date"], run["images"], run["synthetic"]), (date(2026, 11, 1), 10, False))
        fire, smoke = run["classes"]
        self.assertEqual((fire["name"], fire["threshold"], fire["metrics"]["precision"], fire["ap50"]), ("Fire", 0.4, 0.75, 0.7))
        self.assertEqual(fire["imageAccuracy"], 0.8)  # (4 + 4) / 10
        self.assertEqual((smoke["metrics"]["precision"], smoke["imageAccuracy"]), (None, None))  # no detections, no negatives: none

        html = self.client.get("/reports").content.decode()
        self.assertIn('data-run="2026-11-01-fire" data-class="Fire"', html)
        self.assertIn("4 / 1 / 1 / 4", html)
        self.assertIn("0.750", html)

    def test_synthetic_runs_are_apart_a_broken_run_says_why_and_nothing_is_invented_without_runs(self):
        self.assertIn("No evaluation runs recorded.", self.client.get("/reports").content.decode())
        self.write("2026-10-19-stress-synthetic/fire-fog", self.RUN)
        self.write("2026-10-20-broken", "{not json")
        r = self.client.get("/reports")
        self.assertEqual([x["source"] for x in r.context["synthetic"]], ["2026-10-19-stress-synthetic/fire-fog"])
        [broken] = r.context["measured"]
        self.assertIn("Cannot read report.json", broken["error"])

    def test_the_recorded_ai_service_evaluations_are_read_without_errors(self):
        with mock.patch.dict(settings.SENTINEL, AI_EVALUATION_DIR=settings.BASE_DIR.parent / "ai-service" / "reports"):
            runs = services.evaluations()
        if not runs:
            self.skipTest("ai-service/reports is not in this checkout")
        self.assertEqual([r for r in runs if "error" in r], [])
        run = next(r for r in runs if r["source"] == "2026-10-19-fire_smoke-yolox_tiny-dfire-test")
        fire = next(c for c in run["classes"] if c["name"] == "Fire")
        self.assertEqual((run["images"], fire["metrics"]["precision"], fire["metrics"]["recall"], fire["ap50"]), (4306, 0.7313, 0.6487, 0.6852))
