# Google Maps Reviews Scraper (ES)

A Selenium-based scraper that extracts reviews from Google Maps place pages. Given a list of Google Maps URLs it navigates to each place, selects the desired sort order, scrolls through the reviews panel, and writes all reviews to a CSV file.

Two execution modes are available:

- **Mode A (default)** — conservative, safe for long unattended runs. Runs one browser by default; add `--workers N` for moderate parallelism.
- **Mode B (`--turbo`)** — aggressive parallelisation plus browser-level performance flags (images disabled, eager page-load). Maximises throughput at the cost of higher bot-detection risk.

A companion `monitor.py` script incrementally stores new reviews in MongoDB for scheduled/cron runs.

---

## Requirements

| Requirement | Version |
|---|---|
| Python | ≥ 3.9 |
| Google Chrome | installed and in PATH |
| ChromeDriver | must match installed Chrome version |

Install Chrome and ChromeDriver on Debian/Ubuntu:

```bash
# Chrome (if not already installed)
wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | sudo apt-key add -
echo "deb http://dl.google.com/linux/chrome/deb/ stable main" | sudo tee /etc/apt/sources.list.d/google-chrome.list
sudo apt update && sudo apt install -y google-chrome-stable

# ChromeDriver — must match the Chrome version shown by `google-chrome --version`
# Find the matching version at https://chromedriver.chromium.org/downloads
# Then place the binary somewhere on your PATH, e.g. /usr/local/bin/chromedriver
```

---

## Installation

```bash
git clone <repo-url>
cd googlemaps-reviews-scraper-es

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

---

## Usage

### Review scraper

```bash
python3 scraper.py [options]
```

**Options:**

| Flag | Type | Default | Description |
|---|---|---|---|
| `--i` | str | `urls.txt` | Input file: one Google Maps place URL per line |
| `--o` | str | `output.csv` | Output filename — written inside the `data/` folder |
| `--N` | int | `100` | Maximum number of reviews to fetch per URL |
| `--sort_by` | str | `newest` | Sort order (see Sort Modes below) |
| `--place` | flag | off | Extract place metadata instead of reviews |
| `--debug` | flag | off | Show the browser window while scraping |
| `--source` | flag | off | Append an extra `url_source` column to the CSV |
| `--workers` | int | `1` (normal) / `4` (turbo) | Number of parallel Chrome browsers |
| `--turbo` | flag | off | Mode B: aggressive parallelisation + browser optimisations |
| `--log` | str | `scraper_YYYYMMDD_HHMMSS.log` | Log file path (timestamped per run by default) |

See [HYPERPARAMETERS.md](HYPERPARAMETERS.md) for the full parameter reference including internal timing constants and the logging/fault-tolerance system.

---

### Usage examples

#### Single place — 50 reviews, newest first

```bash
python3 scraper.py --N 50 --i urls.txt --o reviews.csv --sort_by newest
```

#### Bulk extraction from a URL list — with source column

```bash
python3 scraper.py --N 200 --i urls.txt --o reviews.csv --sort_by newest --source
```

#### Mode A — default (single browser, safe)

```bash
python3 scraper.py --N 100 --i urls.txt --o output.csv
```

#### Mode A — parallel (3 workers, moderate speedup)

```bash
python3 scraper.py --N 200 --i urls.txt --o output.csv --workers 3
```

#### Mode B — turbo (default 4 workers + browser optimisations)

```bash
python3 scraper.py --N 500 --i urls.txt --o output.csv --turbo
```

#### Mode B — turbo with explicit worker count

```bash
python3 scraper.py --N 500 --i urls.txt --o output.csv --turbo --workers 6
```

#### Place metadata only

```bash
python3 scraper.py --place --i urls.txt
```

#### Debug mode (visible browser window — useful when headless is blocked)

```bash
python3 scraper.py --N 50 --i urls.txt --o debug_out.csv --debug
```

---

### How to get a valid URL

1. Open [Google Maps](https://www.google.com/maps) in your browser.
2. Search for a place and open its page.
3. Copy the URL from the address bar once the place detail panel is open.
4. Paste it into `urls.txt`, one URL per line.

The URL must identify a specific place — it should contain the place name and coordinate segment (`@lat,lng,zoom`). URLs that contain long search-query parameters (`!15s...` or `!1m2!2m1!1s...`) come from search-result views and may be less reliable; prefer URLs obtained by opening a place's own panel directly.

---

## Output

Reviews are written to `data/<output_file>` as CSV. Fields:

| Field | Type | Description |
|---|---|---|
| `id_review` | str | Unique review identifier |
| `caption` | str | Review text (None if rating-only) |
| `relative_date` | str | Original relative date string from Google (e.g. "Hace 3 días") |
| `review_date` | datetime | Approximate absolute date (retrieval_date minus relative duration) |
| `retrieval_date` | datetime | Timestamp when the scrape ran |
| `rating` | float | Star rating (1.0–5.0) |
| `username` | str | Display name of the reviewer |
| `n_review_user` | int/str | Number of reviews the reviewer has posted |
| `n_photo_user` | — | Always empty; Google no longer exposes this publicly |
| `url_user` | str | Reviewer's Google Maps profile URL |

When `--source` is used, an extra `url_source` column is appended with the place URL.

---

## Sort modes

| `--sort_by` value | Google Maps option | What you get |
|---|---|---|
| `most_relevant` | Más relevantes | Google's ranked mix of recent and high-quality reviews |
| `newest` | Más recientes | Chronological, newest first |
| `highest_rating` | Calificación más alta | 5-star reviews first |
| `lowest_rating` | Calificación más baja | 1-star reviews first |

---

## Place metadata mode

With `--place`, the scraper calls `get_account()` instead and prints a dict of place attributes:

```
name, overall_rating, n_reviews, n_photos, category, description,
address, website, phone_number, plus_code, opening_hours, url, lat, long
```

---

## Monitor (incremental scraping)

`monitor.py` runs the scraper on a schedule and stores only new reviews in MongoDB, stopping when it hits a review it has already seen or one older than `--from-date`.

Requires a running MongoDB instance ([installation guide](https://www.mongodb.com/docs/manual/installation/)).

```bash
python3 monitor.py --i urls.txt --from-date 2025-01-01
```

| Flag | Default | Description |
|---|---|---|
| `--i` | `urls.txt` | Input file with place URLs |
| `--from-date` | required | Earliest review date to store (YYYY-MM-DD) |
| `--db-url` | `mongodb://localhost:27017/` | MongoDB connection string |

---

## Fault tolerance and observability

### Incremental output — crash-safe

The output CSV is opened in **append mode** at startup. Every scroll batch (~10 reviews) is flushed to disk immediately after extraction. If the process is killed or crashes:

- All reviews written before the crash are already in the file — nothing is lost.
- Re-run with the same `--o` file to append the missing entries (no duplicate header, no data loss).
- To deduplicate after a double-run: `pandas.read_csv(...).drop_duplicates(subset=['id_review']).to_csv(...)`

### Logging

Every run writes a structured log to `scraper_YYYYMMDD_HHMMSS.log` (or a custom path via `--log`).

Log levels:
- **Console (INFO+)**: one line per URL — STARTED, COMPLETED, SKIPPED, FAILED — and the end-of-run summary.
- **File (DEBUG+)**: adds per-batch progress lines and full exception tracebacks for every FAILED entry.

**Line numbers** in log messages match the 1-indexed line position in the input file, making it unambiguous which entries failed.

**End-of-run summary** (printed to both console and file):

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
  FAILED  line(s) (#)     : 3
  SKIPPED line(s) (#)     : 7

  RESUME HINT: 2 line(s) need attention.
    grep -n "" urls.txt | grep -E "^(3|7):" | cut -d: -f2- > retry.txt
    python3 scraper.py --i retry.txt ...
──────────────────────────────────────────────────────────────────
```

---

## Performance notes

### What was optimised

The main bottleneck in the original scraper was fixed `time.sleep()` calls that accounted for ~50 seconds per URL regardless of actual page speed:

| Location | Before | After |
|---|---|---|
| `sort_by()` setup | 11 s fixed sleep | Dynamic `WebDriverWait` (staleness + reload detection) |
| `get_reviews()` per call | 4 s fixed sleep | Dynamic wait for review count to increase |
| `get_account()` | 2 s fixed sleep | Dynamic wait for `h1` element |
| URL processing | Sequential, 1 browser | Optional parallel (`--workers N`), 1 browser/worker |
| Review re-parsing | O(n²): parsed all N reviews per batch | ID-based dedup, skips known reviews |

### Expected speedup

| Scenario | Before | After (estimate) |
|---|---|---|
| 100 reviews, 1 URL | ~55 s | ~15–25 s |
| 100 reviews × 10 URLs, workers=1 | ~550 s | ~175 s |
| 100 reviews × 10 URLs, workers=4 | ~550 s | ~50–70 s |
| 100 reviews × 10 URLs, workers=4, turbo | ~550 s | ~35–55 s |

Actual times vary with network speed, Google Maps response latency, and machine resources.

### RAM budget for parallel mode

Each headless Chrome instance uses ~200–400 MB. With `--turbo` (images blocked) this drops to ~150–250 MB. A rough guide:

| Available RAM | Recommended max `--workers` |
|---|---|
| 4 GB | 2–3 |
| 8 GB | 4–5 |
| 16 GB | 6–8 |

---

## Known limitations

- **Rate limiting / bot detection**: Google detects automation and may show a "limited view" that hides reviews. The scraper uses anti-detection Chrome flags (`--disable-blink-features=AutomationControlled`, no `navigator.webdriver`, spoofed user-agent) to mitigate this. Higher `--workers` values raise the risk.
- **Headless mode**: `--debug` (visible browser) is sometimes more reliable if headless scraping is blocked.
- **`review_date` is approximate**: Computed by subtracting the relative duration from the retrieval timestamp; may be off by days.
- **`n_photo_user` is always empty**: Google removed this field from the public UI.
- **Class selectors will drift**: Google Maps obfuscates its CSS class names. If scraping suddenly stops working, inspect the constants block at the top of `googlemaps.py` and update the selectors there.
- **No authentication**: Reviews requiring a Google account to view will not be scraped.
- **~10 reviews per scroll**: Each `get_reviews()` call loads approximately 10 reviews. The `--N` loop calls it repeatedly until `N` reviews are collected or the page is exhausted.

---

## Changelog

### 2026-04-22

**Fault tolerance and logging**

- **Incremental CSV writing**: output file is now opened in append mode (`'a'`). Each scroll batch (~10 reviews) is flushed to disk immediately after extraction. A crash or Ctrl-C at any point leaves a valid, fully-written CSV up to that moment. Header is written only when the file is new or empty, so re-runs safely append without duplicate headers.
- **Structured logging** (`setup_logger`): dual-handler logger writes to both console (INFO+) and a timestamped log file (DEBUG+). Per-URL status lines (`STARTED`, `COMPLETED`, `SKIPPED`, `FAILED`) carry 1-indexed input-file line numbers so failures map directly back to the input.
- **`RunStats`**: thread-safe counter class accumulates completed/failed/skipped counts and per-category line lists across workers.
- **End-of-run summary**: printed to both console and log file — total entries, per-status counts, total reviews written, elapsed time, explicit line numbers for every failed/skipped entry, and a ready-to-paste `grep` command to build a retry input file.
- **`--log`**: new CLI flag to specify a custom log filename (default: `scraper_YYYYMMDD_HHMMSS.log`).
- **`print(r)` removed** from `googlemaps.get_reviews()`: per-review dict printing was noisy and redundant given the new logging layer. Batch progress is now logged at DEBUG level from `scraper.py`.

### 2026-04-21

**Performance overhaul — two execution modes**

- **Dynamic waits throughout**: replaced all fixed `time.sleep()` calls in `sort_by()`, `get_reviews()`, `get_account()`, and `get_places()` with `WebDriverWait`-based conditions. The scraper now waits exactly as long as the page needs rather than sleeping a fixed worst-case duration.
  - `sort_by()`: after clicking sort option, uses staleness detection on the first review block then waits for fresh reviews to appear. Falls back to a short fixed sleep only if no review blocks were present yet.
  - `get_reviews()`: waits for the DOM review count to increase after scroll instead of sleeping 4 s unconditionally.
  - `get_account()`: waits for the `h1` place name element instead of sleeping 2 s.
- **Parallel execution**: `scraper.py` now accepts `--workers N`. When N > 1, a `ThreadPoolExecutor` runs N independent Chrome instances concurrently, each processing one URL. Thread-safe CSV writing via `threading.Lock`.
- **Mode B (`--turbo`)**: new CLI flag that combines aggressive parallelism (default 4 workers) with browser-level optimisations — images disabled via Chrome prefs, eager page-load strategy (return on DOMContentLoaded, skip images/fonts), extra performance flags (`--disable-extensions`, `--disable-gpu`, etc.).
- **Review ID dedup**: `get_reviews()` now tracks seen review IDs per URL and skips duplicates. Eliminates the O(n²) re-processing where each batch re-parsed all previously seen reviews to find the new ones.
- **`__get_logger` namespacing**: each scraper instance uses a unique logger name (`googlemaps-scraper-{id}`) to avoid handler accumulation when multiple instances are created in the same process.
- **`HYPERPARAMETERS.md`**: new dedicated reference for all CLI parameters and internal timing constants.

### 2026-04-07

**Fixed: review sorting was completely broken**

- **Root cause 1 — bot detection**: Old `--headless` mode caused Google Maps to show a "vista limitada" (limited view) that hides all reviews and the sort button. Fixed by switching to `--headless=new` and adding standard anti-bot Chrome flags: `--disable-blink-features=AutomationControlled`, `excludeSwitches: ["enable-automation"]`, `useAutomationExtension: False`, spoofed user-agent, and `navigator.webdriver` override via CDP.
- **Root cause 2 — missing tab navigation**: The sort button only appears after the "Opiniones" (Reviews) tab is activated. Fixed by adding `__open_reviews_tab()` which clicks `//button[@role="tab"]` matching "Opiniones" before any sort interaction.

**Refactored**

- Extracted all CSS/XPath selectors and timeouts to a constants block at the top of `googlemaps.py`.
- Replaced deprecated `pandas.DataFrame.append()` with `pd.concat()` in `get_places()`.
- Added `item['n_photo_user'] = None` to `__parse()` so the output dict always has all HEADER fields.
- Sort failures now log the specific URL and attempt count.
- Added `url.strip()` calls to handle trailing newlines when reading URLs from file.
- Added validation of `--sort_by` value at startup with a clear error message.
