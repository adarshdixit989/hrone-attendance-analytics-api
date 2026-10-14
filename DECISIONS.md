# DECISIONS.md

1. **Indexes.** I use a unique index on `employees.emp_code` to protect employee identity, and a unique compound index on `attendance_logs (emp_code, date)` to prevent duplicate daily attendance even during concurrent punch-ins. Department/join-date and join-date indexes support headcount queries. Attendance indexes support date sorting/filtering and employee/date lookups. I would consider a partial index for open punch records if production traffic made punch-out lookups a bottleneck, but I have not added one because the contract also needs other attendance queries.

2. **Punch-in race.** Both requests may check the employee, but the database unique index is the final authority. One insert succeeds with 201; the other gets a duplicate-key error and is returned as 409. There is no unsafe check-then-insert guarantee.

3. **Ties.** The leaderboard uses MongoDB competition ranking on total late minutes. If two employees tie at rank 2, both appear and the next rank is 4. The `limit` filters by rank, so ties can make the result longer than the limit.

4. **Headcount.** Department summary starts from employees who joined by month end and looks up their attendance. This keeps employees with no logs in headcount and excludes future joiners.

5. **100x data.** I would add integration/load tests, inspect real explain plans, and consider pre-aggregated daily summaries if live aggregation latency became too high.

