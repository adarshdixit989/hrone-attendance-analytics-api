# REVIEW.md — starter defects and fixes

| # | Where | Defect and how to reproduce | Fix in this version |
|---|---|---|---|
| 1 | `compute_late_minutes` | Naively combines the punch-in date and shift start; overnight shifts and early-morning punches can be assigned the wrong shift day. | Determine the attendance date under R1, then compare UTC instants against the IST shift start. |
| 2 | `compute_late_minutes` | Grace-boundary logic is not explicit about second truncation; a punch at 09:40:00.900 can be treated inconsistently. | Truncate instants to whole seconds and apply the strict `> 10 minutes` rule. |
| 3 | `compute_work_hours` | Python `round()` uses ties-to-even and binary floats, not the required half-up rule. | Use `Decimal` with `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | Builds shift end on the same date for every shift, so overnight shifts calculate overtime against the wrong instant. | Use the next calendar day for overnight shift ends and apply the 30-minute minimum. |
| 5 | `/health` | Always returns `ok`, even if MongoDB is down. | Ping MongoDB and return 503 on failure. |
| 6 | `create_employee` | Check-then-insert is race-prone; duplicate requests can both pass the check. It also did not validate shift inequality or enforce a database uniqueness guarantee. | Validate the request and rely on a unique `emp_code` index; map duplicate-key errors to 409. |
| 7 | `list_employees` | `skip = page * page_size` skips page 1, and `total` ignores the department filter. Results were not explicitly sorted. | Use `(page - 1) * page_size`, count the filtered query, sort by `emp_code`. |
| 8 | `punch_in` | Does not return 404 for an unknown employee, can crash when employee lookup is empty, uses local machine time, and does not validate status/timestamp. | Validate employee and payload; use UTC instants and IST calendar rules. |
| 9 | `punch_in` | Check-then-insert alone cannot guarantee one record per employee/date under concurrent requests. | Enforce unique `(emp_code, date)` in MongoDB and handle `DuplicateKeyError` as 409. |
| 10 | `punch_in` / serializers | Returned Mongo `_id` as an `id`, and instants were not converted to epoch milliseconds. | Keep `_id` internal and serialize every instant (including history values) to epoch milliseconds. |
| 11 | `list_attendance` | Loads all matching records into Python, sorts/slices in memory, leaks `_id`, and does not validate date ranges. | Count and paginate in MongoDB with a stable indexed sort and validate inclusive date filters. |
| 12 | Attendance model | The starter omitted punch-out, regularization, analytics and explain endpoints. | Implement all contract paths in `app/main.py`. |
| 13 | Punch-out | A read-then-write without a conditional update allows simultaneous punch-outs to overwrite each other; overnight records are easy to miss. | Select the latest punch-in at/before the supplied instant and conditionally update only while `punch_out` is null. |
| 14 | Regularization | Corrections need derived-field recomputation and append-only audit entries; concurrent edits can lose history. | Recompute derived fields, record only changed fields, and compare-and-swap against the existing history. |
| 15 | Analytics | Python-side collection scans/loops do not scale and can produce incorrect counts for weekends, half-days, ties, join dates, or employees without logs. | Use MongoDB aggregation pipelines, date-aware filters, competition ranking, and employee-driven department headcount/trend aggregation. |

## Reviewed and intentionally retained

- `emp_code` is the business identifier; MongoDB `_id` must not be exposed.
- `date` and `joined_on` remain `YYYY-MM-DD` strings, while actual instants are BSON datetimes in storage.
- Missing legacy `history`, `half_day`, `late_minutes`, and `overtime_minutes` are treated as empty/default values.
- The sample dataset is illustrative only; hidden cases require testing beyond these nine records.

