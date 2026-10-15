# Implementation Decisions

## 1. Indexes

Unique indexes on `employees.emp_code` and attendance `(emp_code, date)` prevent duplicate employees and daily punches. Other indexes support employee/date lookups, attendance sorting and status filtering, recent punch-in searches, and department headcount queries. I rejected indexes on derived values because no endpoint filters or sorts on them, and each index adds write cost.

## 2. Punch-in race

Concurrent requests may both find no existing record, but the unique index allows only one insert. The duplicate-key error becomes 409. Punch-out updates only a record with `punch_out: null`, so only one request can close it.

## 3. Leaderboard ties

Ranking is by total late minutes. The limit is applied after ranking, so all employees tied at rank 1 are returned when the limit is 1. Tied rows are ordered by employee code.

## 4. Department headcount

The summary starts with employees who joined by month end, then looks up their logs. Employees with no logs count once; employees who join later are excluded.

## 5. Scaling to 100 times the data

I would benchmark the aggregations on representative data and consider scheduled summary collections for expensive reports, keeping the API response contract unchanged.
