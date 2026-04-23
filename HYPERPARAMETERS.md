# Hyperparameter Reference

This document covers every tunable parameter exposed by `scraper.py` and `monitor.py`, plus the internal timing constants in `googlemaps.py` that control how the scraper behaves under the hood.

---

## Execution Modes

The scraper has two execution modes. The right choice depends on how many URLs you are scraping and how much rate-limit risk is acceptable.

| Mode | How to activate | Worker default | Browser optimisations | Risk level |
|---|---|---|---|---|
| **Mode A — Default** | No extra flags | 1 | None | Low |
| **Mode A — Parallel** | `--workers N` (N ≥ 2) | Explicit | None | Medium |
| **Mode B — Turbo** | `--turbo` | 4 | Images blocked, eager page-load | High |

**When to use Mode A (single):** scraping a handful of URLs (< 20), running unattended overnight, or when you want to minimise any chance of bot detection.

**When to use Mode A (parallel, `--workers 2-3`):** a moderate batch (20–100 URLs) where you want a meaningful speedup without spawning many Chrome instances.

**When to use Mode B (`--turbo`):** large batches (100+ URLs) where throughput matters more than caution. Expect occasional rate-limit blocks; re-run with a fresh IP session if Google shows CAPTCHAs.

---

## `scraper.py` — CLI Parameters

| Parameter | Type | Default | Valid range / values | Description |
|---|---|---|---|---|
| `--N` | int | `100` | 1 – ∞ | Maximum reviews fetched **per URL**. The loop stops early if Google returns no more reviews. |
| `--i` | str | `urls.txt` | any readable file | Input file. One Google Maps place URL per line. Blank lines are skipped. |
| `--o` | str | `output.csv` | any filename | Output filename. Written to `data/<filename>`. |
| `--sort_by` | str | `newest` | `most_relevant` `newest` `highest_rating` `lowest_rating` | Sort order applied before scraping. See sort modes below. |
| `--place` | flag | off | — | Extract **place metadata** (name, rating, address, hours…) instead of reviews. |
| `--debug` | flag | off | — | Show the browser window. Useful for diagnosing bot-detection blocks. Cannot be combined with `--turbo` meaningfully (turbo is headless-only). |
| `--source` | flag | off | — | Append an extra `url_source` column to the CSV. Useful when scraping many URLs in one run. |
| `--workers` | int | `1` (normal) / `4` (turbo) | ≥ 1 | Number of parallel Chrome browsers. Each worker processes one URL at a time. Setting `--workers 1` forces single-threaded mode even with `--turbo`. |
| `--turbo` | flag | off | — | **Mode B.** Enables browser performance flags (images disabled, eager page-load strategy) and raises the default worker count to 4. Accepts higher rate-limit risk. |
| `--log` | str | `scraper_YYYYMMDD_HHMMSS.log` | any filename | Log file path. Written in the current working directory. Defaults to a timestamped filename so each run gets its own log. |

### Speed vs stability tradeoffs

```
--workers 1                  Safest. Slowest. One Chrome, sequential.
--workers 2 or 3             Good balance. ~2–3× throughput vs single.
--workers 4  (--turbo default) Fast. Four Chromes. Risk of bot detection rises.
--workers 6+                 Maximum throughput. High memory use (~1 GB/worker).
                             Only use on a machine with ≥ 8 GB RAM and good
                             bandwidth. Expect some workers to be rate-limited.
```

> **RAM budget:** each Chrome instance uses roughly 200–400 MB in headless mode. With `--turbo` (images disabled) this drops to ~150–250 MB. Do not exceed `floor(free_RAM_GB / 0.3)` workers.

---

## Fault Tolerance

### Incremental CSV writing

The output file is opened in **append mode** from the very first write. Every scroll batch is flushed to disk immediately after being written. This means:

- **Crash or Ctrl-C mid-run**: all reviews extracted before the crash are already in the file. Nothing is lost.
- **Resuming a partial run**: re-run with the same `--o` file. Because the file is opened in append mode and the header is only written to empty files, rows will be appended without duplicating the header.
- **Deduplication on resume**: the output CSV may contain duplicate rows if the same URL is scraped again on resume. Post-process with `pandas.DataFrame.drop_duplicates(subset=['id_review'])` to clean.

### Logging system

Each run produces a structured log file (default: `scraper_YYYYMMDD_HHMMSS.log`).

**Per-URL log lines** (always written):

```
2026-04-22 10:35:01  INFO      [#0001] STARTED   https://maps.google.com/...
2026-04-22 10:35:44  INFO      [#0001] COMPLETED  https://...  reviews=47
2026-04-22 10:36:10  WARNING   [#0002] SKIPPED   https://...  reason=sort_by_failed
2026-04-22 10:36:55  WARNING   [#0003] FAILED    https://...  error=TimeoutException(...)
```

**Per-batch debug lines** (file only, not console):

```
2026-04-22 10:35:12  DEBUG     [#0001] batch  offset=0  size=10
2026-04-22 10:35:22  DEBUG     [#0001] batch  offset=10  size=10
```

**Full tracebacks** (file only, DEBUG level):

```
2026-04-22 10:36:55  DEBUG     [#0003] TRACEBACK:
Traceback (most recent call last):
  ...
selenium.common.exceptions.TimeoutException: ...
```

**End-of-run summary** (both console and file):

```
──────────────────────────────────────────────────────────────────
RUN SUMMARY
  Input entries  total    : 10
  Completed               : 8
  Failed                  : 1
  Skipped                 : 1
  Total reviews written   : 742
  Last completed line (#) : 9
  Elapsed time            : 0:14:33
  Log file                : scraper_20260422_103501.log
  FAILED  line(s) (#)     : 3
  SKIPPED line(s) (#)     : 7

  RESUME HINT: 2 line(s) need attention.
  Extract those lines from the input file and re-run:
    grep -n "" urls.txt | grep -E "^(3|7):" | cut -d: -f2- > retry.txt
    python3 scraper.py --i retry.txt ...
──────────────────────────────────────────────────────────────────
```

**Line numbers** in all log messages refer to the actual 1-indexed line position in the input file (blank lines included in the count), making it unambiguous which entries to re-run.

---

## `monitor.py` — CLI Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `--i` | str | `urls.txt` | Input file with place URLs. |
| `--from-date` | str | required | Earliest review date to store (format: `YYYY-MM-DD`). Scraping stops for a URL when a review older than this date is encountered. |
| `--db-url` | str | `mongodb://localhost:27017/` | MongoDB connection string. |

`monitor.py` runs single-threaded (one browser, one URL at a time). It stores new reviews incrementally in MongoDB and stops each URL when it hits an already-seen review or a review older than `--from-date`.

---

## Internal Timing Constants (`googlemaps.py`)

These are not CLI parameters but can be tuned by editing the constants block at the top of `googlemaps.py` to adjust how long the scraper waits for page events.

| Constant | Normal value | Turbo value | What it controls |
|---|---|---|---|
| `MAX_WAIT` | 10 s | 10 s | `WebDriverWait` timeout for initial element lookups (tab, sort button). |
| `MAX_RETRY` | 5 | 5 | How many times to retry clicking the sort button before giving up. |
| `AJAX_TIMEOUT_NORMAL` / `AJAX_TIMEOUT_TURBO` | 8 s | 5 s | Max wait for new review cards to appear after a scroll. Lower = faster but may miss slow-loading batches. |
| `SORT_RELOAD_TIMEOUT` / `SORT_RELOAD_TIMEOUT_TURBO` | 12 s | 8 s | Max wait for the reviews list to reload after selecting a sort option. |
| `ACCOUNT_WAIT_TIMEOUT` / `ACCOUNT_WAIT_TIMEOUT_TURBO` | 10 s | 6 s | Max wait for the place name (`h1`) to appear in `get_account()`. |
| `RETRY_SLEEP_NORMAL` / `RETRY_SLEEP_TURBO` | 1.0 s | 0.5 s | Sleep between sort-button retry attempts (prevents tight spin loops). |
| `SORT_FALLBACK_SLEEP` / `SORT_FALLBACK_SLEEP_TURBO` | 2.0 s | 1.0 s | Fixed fallback sleep used when the dynamic sort-reload wait times out. |
| `PLACES_FALLBACK_SLEEP` / `PLACES_FALLBACK_SLEEP_TURBO` | 1.0 s | 0.3 s | Fallback sleep per search point in `get_places()` if place links don't appear within `AJAX_TIMEOUT`. |

**Guidance:** if you see reviews being missed (empty batches returned before the list is exhausted), increase `AJAX_TIMEOUT_*`. If the scraper is stable and you want more speed, lower it.

---

## Sort Modes

| `--sort_by` value | Google Maps label | Use case |
|---|---|---|
| `most_relevant` | Más relevantes | Google's quality-ranked mix. Best for representative samples. |
| `newest` | Más recientes | Chronological descending. Best for incremental/monitoring runs. |
| `highest_rating` | Calificación más alta | 5-star reviews first. |
| `lowest_rating` | Calificación más baja | 1-star reviews first. |

---

## Quick Reference

```bash
# Minimal single-URL run
python3 scraper.py --N 50 --i urls.txt --o result.csv

# Bulk run — moderate parallelism (Mode A, 3 workers)
python3 scraper.py --N 200 --i urls.txt --o result.csv --sort_by newest --source --workers 3

# Maximum throughput (Mode B — turbo, 6 workers)
python3 scraper.py --N 500 --i urls.txt --o result.csv --sort_by newest --turbo --workers 6

# Place metadata only
python3 scraper.py --place --i urls.txt

# Incremental monitoring into MongoDB
python3 monitor.py --i urls.txt --from-date 2025-01-01
```
