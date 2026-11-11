# diffwise Benchmark Results

- **Date:** 2026-11-11 01:00 UTC
- **Backend:** `ollama`
- **Model:** `qwen2.5-coder:7b`
- **Cases:** 14
- **Caught:** 7/14 (50.0%)
- **Exact Severity Match:** 6/14
- **Missed:** 7/14
- **False Positives (non-seeded findings):** 2
- **Total Runtime:** 145.1s (avg 10.4s/diff)

## Per-Case Results

| ID | Seeded Issue | File | Expected | Result | Reported Severity | False Positives | Latency | Findings |
| --- | --- | --- | --- | --- | --- | ---: | ---: | --- |
| `01_sql_injection` | SQL injection | `src/db.py` | security | Caught | security | 0 | 14.2s | [security] L12: SQL Injection Vulnerability |
| `02_hardcoded_secret` | Hardcoded secret | `src/auth.py` | security | Caught | security | 0 | 10.9s | [security] L4: Hardcoded secret |
| `03_off_by_one` | Off-by-one error | `src/pagination.py` | bug | Caught | bug | 0 | 14.7s | [bug] L11: Off-by-one error in loop |
| `04_unhandled_exception` | Unhandled exception | `src/webhook.py` | bug | Missed | - | 0 | 5.8s | None |
| `05_command_injection` | Command injection | `src/media.py` | security | Caught | security | 0 | 13.5s | [security] L5: Insecure use of shell=True |
| `06_path_traversal` | Path traversal | `src/files.py` | security | Missed | - | 1 | 11.2s | [security] L3: Hardcoded file path |
| `07_resource_leak` | Resource leak | `src/exporter.py` | bug | Caught | bug | 0 | 15.8s | [bug] L12: Potential file handle leak |
| `08_mutable_default` | Mutable default argument | `src/tags.py` | bug | Missed | - | 0 | 5.7s | None |
| `09_null_dereference` | Null dereference | `src/profile.ts` | bug | Missed | - | 0 | 5.9s | None |
| `10_dom_xss` | DOM XSS | `src/banner.js` | security | Missed | - | 0 | 5.8s | None |
| `11_toctou_race` | Race condition (TOCTOU) | `src/ledger.py` | bug | Missed | - | 1 | 11.7s | [bug] L15: Unnecessary balance update |
| `12_infinite_loop` | Infinite loop / unreachable code | `src/retry.py` | bug | Missed | - | 0 | 5.9s | None |
| `13_weak_password_hash` | Weak password hashing | `src/passwords.py` | security | Caught | security | 0 | 11.8s | [security] L5: Insecure hash function |
| `14_missing_timeout` | Missing HTTP timeout | `src/client.py` | bug | Caught | suggestion | 0 | 12.2s | [suggestion] L5: Unlimited timeout |

## Scoring Methodology

- Each benchmark case applies a real unified `.diff` to an isolated temporary Git repository and runs `diffwise --format json` end-to-end.
- **Caught:** At least one structured JSON finding targets the seeded file within +/- 2 lines of the seeded defect (or file-level) and matches the case's semantic keyword groups.
- **False Positives:** Any structured finding emitted on the diff that does not match the seeded issue.
- Plain-text JSON parse fallbacks are scored conservatively as **Missed (JSON fallback)** rather than guessed as caught.
