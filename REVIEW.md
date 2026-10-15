# Starter Code Review

I reviewed the supplied starter against the API contract, data model, and business rules R1–R10. The defects below are in the original starter; fixes are implemented in `app/main.py`.

| # | Location | Defect and how to detect it | Correction |
|---:|---|---|---|
| 1 | `health` | Returns 200 even if MongoDB is unavailable. Stop MongoDB and the probe still reports healthy. | Ping MongoDB and return 503 on failure. |
| 2 | `compute_late_minutes` | Uses naive local time and does not explicitly truncate fractional seconds. Results can depend on the host timezone. | Convert instants to aware UTC, truncate to whole seconds, then calculate in IST. |
| 3 | `compute_overtime` | Counts any positive overtime instead of requiring at least 30 minutes; uses the wrong end date for overnight shifts. | Apply the 30-minute threshold and place overnight shift end on the next date. |
| 4 | `compute_work_hours` | Binary float rounding can differ from the required decimal half-up rule. Test a duration exactly halfway between hundredths. | Use `Decimal` with `ROUND_HALF_UP`. |
| 5 | Employee validation and `create_employee` | Accepts invalid field formats and equal shift times; check-then-insert cannot prevent concurrent duplicates. | Validate the request, create a unique `emp_code` index, and map duplicate-key errors to 409. |
| 6 | `create_employee` | Stores a naive timestamp, which makes the instant dependent on host timezone. | Store an aware UTC datetime and return epoch milliseconds. |
| 7 | `list_employees` | Uses `page * page_size` as the offset, counts before applying the filter, and has no stable sort or page-size limit. Page 1 skips its first page. | Use `(page - 1) * page_size`, count filtered records, sort by `emp_code`, and validate pagination. |
| 8 | `punch_in` | An unknown employee can trigger a server error; arbitrary statuses and invalid timestamps are accepted. | Return 404 for unknown employees and validate status and epoch milliseconds. |
| 9 | `punch_in` | Uses host-local time, misses the overnight attendance-date rule, and stores fractional seconds. Check UTC/IST boundary, overnight, and millisecond inputs. | Truncate and convert timestamps correctly, derive the IST shift date, and enforce a unique `(emp_code, date)` index. |
| 10 | `list_attendance` | Loads all matches into Python, sorts only by date, and does not validate date ranges or pagination bounds. This scales poorly and same-date order is unstable. | Filter, count, sort, skip, and limit in MongoDB using indexed queries and a stable sort. |
| 11 | Missing punch-out route | Open records cannot be closed and derived work, overtime, and half-day values are never calculated. | Add an atomic update that only closes a record whose `punch_out` is null. |
| 12 | Missing regularization route | Corrections, derived-value recomputation, and audit history are not implemented. | Validate corrections, recompute derived values, append a history entry, and reject stale concurrent updates. |
| 13 | Missing analytics and explain routes | Monthly summaries, department reports, tied rankings, gap-filled trends, and query plans are unavailable. | Add the contract routes, aggregation pipelines, and supporting indexes. |

## Reviewed and retained

- A fixed UTC+05:30 timezone correctly represents IST for this assignment; India has no daylight-saving changes.
- BSON datetimes are appropriate for instants, while attendance dates, join dates, and shift times remain strings as specified by the data model.
