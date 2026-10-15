"""End-to-end regression checks. Run with: python submission/tests/test_api.py"""
import json
import os
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["MONGO_DB"] = "attendance_codex_test_" + uuid4().hex[:10]
TEST_DB_NAME = os.environ["MONGO_DB"]

import uvicorn
from pymongo import MongoClient
from app.main import app, db


def ms(value: str) -> int:
    return int(datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp() * 1000)


class AttendanceApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        deadline = time.time() + 15
        while not cls.server.started and time.time() < deadline:
            time.sleep(0.05)
        if not cls.server.started:
            raise RuntimeError("test API did not start")
        port = cls.server.servers[0].sockets[0].getsockname()[1]
        cls.base = f"http://127.0.0.1:{port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=5)
        cleanup_client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
        cleanup_client.drop_database(TEST_DB_NAME)
        cleanup_client.close()

    def request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read())
            finally:
                error.close()

    def create_employee(self, code, department="Engineering"):
        return self.request("POST", "/employees", {
            "emp_code": code, "name": code, "email": code.lower() + "@example.test",
            "department": department, "shift_start": "09:30", "shift_end": "18:30",
            "joined_on": "2026-01-01",
        })

    def test_full_contract_and_concurrency_smoke(self):
        status, data = self.request("GET", "/health")
        self.assertEqual((status, data), (200, {"status": "ok"}))
        status, spec = self.request("GET", "/openapi.json")
        self.assertEqual(status, 200)
        self.assertIn("/attendance/{emp_code}/{date}", spec["paths"])

        for code in ("EMP9001", "EMP9002", "EMP9003"):
            self.assertEqual(self.create_employee(code)[0], 201)
        self.assertEqual(self.create_employee("EMP9001")[0], 409)
        self.assertEqual(self.create_employee("BAD")[0], 422)
        self.assertEqual(self.request("POST", "/attendance/punch-in", {"emp_code": "EMP9999"})[0], 404)
        self.assertEqual(self.request("POST", "/attendance/punch-out", {"emp_code": "EMP9999"})[0], 404)

        # Grace is strict: exactly ten minutes is on time; one second more is late.
        self.assertEqual(self.create_employee("EMP9005", "QA")[0], 201)
        self.assertEqual(self.create_employee("EMP9006", "QA")[0], 201)
        grace = ms("2026-10-12T04:10:00")
        _, on_grace = self.request("POST", "/attendance/punch-in", {"emp_code": "EMP9005", "punched_at": grace})
        _, past_grace = self.request("POST", "/attendance/punch-in", {"emp_code": "EMP9006", "punched_at": grace + 1000})
        self.assertEqual(on_grace["late_minutes"], 0)
        self.assertEqual(past_grace["late_minutes"], 10)

        # Half-up hour rounding controls the half-day cutoff.
        self.assertEqual(self.create_employee("EMP9007", "QA")[0], 201)
        self.assertEqual(self.create_employee("EMP9008", "QA")[0], 201)
        day_start = ms("2026-10-14T04:00:00")
        self.assertEqual(self.request("POST", "/attendance/punch-in", {"emp_code": "EMP9007", "punched_at": day_start, "status": "WFH"})[0], 201)
        _, rounded_full = self.request("POST", "/attendance/punch-out", {"emp_code": "EMP9007", "punched_at": day_start + (4 * 3600 + 29 * 60 + 42) * 1000})
        self.assertEqual(rounded_full["work_hours"], 4.5)
        self.assertFalse(rounded_full["half_day"])
        self.assertEqual(self.request("POST", "/attendance/punch-in", {"emp_code": "EMP9008", "punched_at": day_start})[0], 201)
        _, rounded_half = self.request("POST", "/attendance/punch-out", {"emp_code": "EMP9008", "punched_at": day_start + (4 * 3600 + 29 * 60 + 41) * 1000})
        self.assertEqual(rounded_half["work_hours"], 4.49)
        self.assertTrue(rounded_half["half_day"])

        punch_at = ms("2026-10-12T04:15:00")  # 09:45 IST, 15 minutes late
        def do_punch_in(_):
            return self.request("POST", "/attendance/punch-in", {"emp_code": "EMP9001", "punched_at": punch_at})[0]
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(do_punch_in, range(12)))
        self.assertEqual(results.count(201), 1)
        self.assertEqual(results.count(409), 11)

        punch_out_at = ms("2026-10-12T13:45:00")  # 19:15 IST: 9.5 h and 45 min overtime
        def do_punch_out(_):
            return self.request("POST", "/attendance/punch-out", {"emp_code": "EMP9001", "punched_at": punch_out_at})[0]
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(do_punch_out, range(12)))
        self.assertEqual(results.count(200), 1)
        self.assertEqual(results.count(409), 11)
        _, record = self.request("GET", "/attendance?emp_code=EMP9001&date_from=2026-10-12&date_to=2026-10-12")
        self.assertEqual(record["total"], 1)
        self.assertEqual(record["items"][0]["late_minutes"], 15)
        self.assertEqual(record["items"][0]["work_hours"], 9.5)
        self.assertEqual(record["items"][0]["overtime_minutes"], 45)
        self.assertNotIn("_id", record["items"][0])

        # Tied late minutes must share rank 1 and both survive limit=1.
        self.assertEqual(self.request("POST", "/attendance/punch-in", {"emp_code": "EMP9003", "punched_at": punch_at})[0], 201)
        self.assertEqual(self.request("POST", "/attendance/punch-out", {"emp_code": "EMP9003", "punched_at": punch_out_at})[0], 200)
        _, board = self.request("GET", "/analytics/leaderboard/late?month=2026-10&limit=1")
        self.assertEqual([row["emp_code"] for row in board["items"]], ["EMP9001", "EMP9003"])
        self.assertEqual([row["rank"] for row in board["items"]], [1, 1])

        # A manual correction appends an audit entry and recomputes late time.
        start = ms("2026-10-13T04:00:00")
        self.assertEqual(self.request("POST", "/attendance/punch-in", {"emp_code": "EMP9001", "punched_at": start})[0], 201)
        status, corrected = self.request("PATCH", "/attendance/EMP9001/2026-10-13", {
            "punch_in": ms("2026-10-13T04:15:00"), "reason": "corrected source time", "regularized_by": "qa"})
        self.assertEqual(status, 200)
        self.assertEqual(corrected["late_minutes"], 15)
        self.assertEqual(len(corrected["history"]), 1)
        self.assertEqual(corrected["history"][0]["changes"]["punch_in"]["from"], start)

        # Simultaneous corrections either conflict or append in sequence; neither may erase history.
        def regularize(out_at):
            return self.request("PATCH", "/attendance/EMP9001/2026-10-13", {
                "punch_out": out_at, "reason": "parallel adjustment", "regularized_by": "qa"})[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            correction_statuses = list(pool.map(regularize, [ms("2026-10-13T12:00:00"), ms("2026-10-13T12:01:00")]))
        self.assertTrue(all(code in (200, 409) for code in correction_statuses))
        self.assertEqual(correction_statuses.count(200), 1 if 409 in correction_statuses else 2)
        _, corrected_again = self.request("GET", "/attendance?emp_code=EMP9001&date_from=2026-10-13&date_to=2026-10-13")
        self.assertEqual(len(corrected_again["items"][0]["history"]), 1 + correction_statuses.count(200))

        # The department summary must include the employee with no logs.
        _, summary = self.request("GET", "/analytics/departments/summary?month=2026-10&department=Engineering")
        engineering = summary["items"][0]
        self.assertEqual(engineering["headcount"], 3)
        self.assertEqual(engineering["present_days"], 3.0)
        _, monthly = self.request("GET", "/analytics/employees/EMP9001/monthly?month=2026-10")
        self.assertEqual(monthly["working_days"], 22)
        self.assertEqual(monthly["present_days"], 2.0)
        self.assertEqual(monthly["total_late_minutes"], 30)

        self.assertEqual(self.request("POST", "/attendance/punch-in", {
            "emp_code": "EMP9001", "punched_at": ms("2026-10-17T04:00:00"), "status": "WFH"})[0], 201)

        _, trend = self.request("GET", "/analytics/departments/Engineering/trend?from=2026-10-12&to=2026-10-18")
        self.assertEqual(len(trend["items"]), 7)
        self.assertEqual(trend["items"][0]["attendance_rate"], 0.6667)
        self.assertEqual(trend["items"][5]["present_count"], 1)
        self.assertIsNone(trend["items"][5]["attendance_rate"])

        self.assertEqual(self.create_employee("EMP9004", "Support")[0], 201)
        db.employees.update_one({"emp_code": "EMP9004"}, {"$set": {"shift_start": "22:00", "shift_end": "06:00"}})
        overnight_in = ms("2026-10-12T17:30:00")  # 23:00 IST
        overnight_out = ms("2026-10-13T01:00:00")  # 06:30 IST, exactly 30 minutes overtime
        self.assertEqual(self.request("POST", "/attendance/punch-in", {"emp_code": "EMP9004", "punched_at": overnight_in})[0], 201)
        _, overnight = self.request("POST", "/attendance/punch-out", {"emp_code": "EMP9004", "punched_at": overnight_out})
        self.assertEqual(overnight["date"], "2026-10-12")
        self.assertEqual(overnight["late_minutes"], 60)
        self.assertEqual(overnight["work_hours"], 7.5)
        self.assertEqual(overnight["overtime_minutes"], 30)

        explains = [
            "/admin/explain/attendance_list?date_from=2026-10-12&date_to=2026-10-18",
            "/admin/explain/employee_monthly?emp_code=EMP9001&month=2026-10",
            "/admin/explain/department_summary?month=2026-10&department=Engineering",
            "/admin/explain/late_leaderboard?month=2026-10&limit=1",
            "/admin/explain/department_trend?department=Engineering&from=2026-10-12&to=2026-10-18",
        ]
        for path in explains:
            self.assertEqual(self.request("GET", path)[0], 200, path)

        self.assertEqual(self.request("POST", "/attendance/punch-in", {"emp_code": "EMP9001", "punched_at": 1792080000})[0], 422)
        self.assertEqual(self.request("GET", "/employees?page_size=101")[0], 422)
        self.assertEqual(self.request("GET", "/attendance?date_from=2026-10-20&date_to=2026-10-01")[0], 422)


if __name__ == "__main__":
    unittest.main(verbosity=2)
