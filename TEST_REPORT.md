# Test Report

**Run date:** 2026-10-15 (Asia/Calcutta)
**Candidate:** `app/main.py`

## Result

**Passed.** `python tests/test_api.py` completed with one end-to-end test method and all assertions passing. Python compilation of the application and test runner also passed.

The separate large-data plan check also passed on 2026-10-15. It loaded 500 employees and 100,000 attendance records into a randomly named disposable local MongoDB database, then checked all five explain endpoints. The four collection-based endpoints showed indexed plans; the department trend aggregation reported indexes `department_joined_on`, `attendance_date_employee`, and `uq_employee_code`, with zero collection scans. The check dropped its temporary database afterward.

The run started the candidate API on an available local port and used a randomly named MongoDB database. The test runner dropped that database during cleanup. It did not write to the existing server's database.

## Coverage

- Health response and required route publication
- Employee validation, duplicate handling, and unknown-employee errors
- Concurrent duplicate punch-ins and concurrent punch-outs
- The 10-minute late grace boundary and whole-minute late calculation
- Half-up work-hour rounding and the half-day cutoff
- Overnight attendance date, work duration, and overtime threshold
- Attendance filtering and internal MongoDB ID omission
- Tied competition ranks with a cutoff limit
- Manual correction history and concurrent corrections
- Monthly totals and department headcount that includes an employee with no logs
- Seven-day department trend, including a weekend record
- Successful responses from all five explain endpoints
- Invalid timestamp, pagination, and date-range responses

## Not covered

The explain check verified the selected plan/index metadata, not latency targets or every possible parameter combination. It does not compare results against the hidden grader dataset. The 100,000 records were synthetic test data, not real attendance data.
