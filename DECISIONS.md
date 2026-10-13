# DECISIONS.md

1. **Indexes.** Unique index on `employees.emp_code` enforces identity uniqueness; compound unique index on `attendance_logs(emp_code, date)` prevents duplicate daily logs and serializes punches. We added `attendance_logs(date: -1, emp_code: 1)` for paginated listing, `attendance_logs(date: 1, late_minutes: 1)` for the leaderboard, and `employees(department: 1, joined_on: 1)` for department filtering. We rejected an index on `late_minutes` alone because queries filter on `date` first, making date-prefixed indexes far more selective.

2. **Punch-in race.** Both concurrent requests attempt `insert_one`. MongoDB's unique index on `(emp_code, date)` serializes the write: the first request succeeds (201), while the second trips the unique constraint and raises `DuplicateKeyError`. Our handler catches this error and returns 409 Conflict.

3. **Ties.** We use competition ranking (`$rank`), so ties share a rank and skip subsequent ranks (e.g., 1, 2, 2, 4). Filtering on `rank <= limit` after ranking includes all employees tied at the cutoff, which can yield more than `limit` rows. Tied rows sort by `emp_code`.

4. **Headcount.** The pipeline drives from `employees` matching `joined_on <= month_end`, then performs a `$lookup` to attendance logs. Employees with zero logs still yield documents with empty arrays, preserving their inclusion in department headcount.

5. **One thing you would change for 100x data.** Pre-aggregate daily and monthly metrics incrementally using background jobs or MongoDB change streams into summary collections, avoiding costly multi-stage pipelines and lookups across millions of raw logs.
