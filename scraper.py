# -*- coding: utf-8 -*-

import argparse
import csv
import logging
import os
import sys
import threading
import traceback as tb_module
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from googlemaps import GoogleMapsScraper
from termcolor import colored


ind = {'most_relevant': 0, 'newest': 1, 'highest_rating': 2, 'lowest_rating': 3}

HEADER = ['id_review', 'caption', 'relative_date', 'review_date', 'retrieval_date',
          'rating', 'username', 'n_review_user', 'n_photo_user', 'url_user']
HEADER_W_SOURCE = HEADER + ['url_source']

DEFAULT_WORKERS_NORMAL = 1
DEFAULT_WORKERS_TURBO  = 4


# ── Incremental CSV output ────────────────────────────────────────────────────

def open_output_csv(source_field, outpath):
    """
    Open the output CSV in *append* mode.

    The header row is written only when the file is newly created or empty,
    which means:
      - A fresh run creates the file and writes the header normally.
      - A resumed run appends rows without a duplicate header.
      - A mid-run crash leaves all already-written rows intact and recoverable.

    Returns (csv.writer, file_handle).
    Callers MUST call file_handle.flush() after each batch to guarantee
    crash-safe incremental persistence.
    """
    path = 'data/' + outpath
    is_new = not os.path.isfile(path) or os.path.getsize(path) == 0
    fh = open(path, mode='a', encoding='utf-8', newline='\n')
    writer = csv.writer(fh, quoting=csv.QUOTE_MINIMAL)
    if is_new:
        writer.writerow(HEADER_W_SOURCE if source_field else HEADER)
        fh.flush()
    return writer, fh


# ── Thread-safe run statistics ────────────────────────────────────────────────

class RunStats:
    """Accumulates counters across threads.  All mutations go through a lock."""

    def __init__(self):
        self._lock            = threading.Lock()
        self.completed        = 0
        self.failed           = 0
        self.skipped          = 0
        self.total_reviews    = 0
        # Track individual line numbers so the summary can list them explicitly
        self.completed_lines  = []
        self.failed_lines     = []
        self.skipped_lines    = []

    def record_completed(self, line_no, n_reviews):
        with self._lock:
            self.completed += 1
            self.total_reviews += n_reviews
            self.completed_lines.append(line_no)

    def record_failed(self, line_no):
        with self._lock:
            self.failed += 1
            self.failed_lines.append(line_no)

    def record_skipped(self, line_no):
        with self._lock:
            self.skipped += 1
            self.skipped_lines.append(line_no)

    @property
    def last_completed_line(self):
        """Highest line number that completed successfully (input-file order)."""
        return max(self.completed_lines) if self.completed_lines else None


# ── Logging setup ─────────────────────────────────────────────────────────────

def setup_logger(log_filename):
    """
    Configure a logger that writes to both a file (DEBUG+) and stdout (INFO+).

    The file receives full tracebacks and per-batch debug lines.
    The console shows only INFO-level status changes and the final summary,
    keeping live output readable.
    """
    logger = logging.getLogger('scraper')
    logger.setLevel(logging.DEBUG)
    logger.propagate = False          # don't leak into the root logger

    fmt = logging.Formatter(
        '%(asctime)s  %(levelname)-8s  %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S')

    fh = logging.FileHandler(log_filename, encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


# ── Core scraping logic ───────────────────────────────────────────────────────

def _scrape_url(url, line_no, scraper, args, writer, file_handle, write_lock, stats, logger):
    """
    Scrape one URL with an already-open scraper instance.

    Emits structured log lines:
      [#NNNN] STARTED   <url>
      [#NNNN] COMPLETED <url>  reviews=N
      [#NNNN] SKIPPED   <url>  reason=...
      [#NNNN] FAILED    <url>  error=...

    Flushes the output file to disk after every scroll batch so that a crash
    at any point leaves a valid, fully-written CSV up to that moment.
    """
    logger.info('[#%04d] STARTED   %s', line_no, url)

    try:
        if args.place:
            result = scraper.get_account(url)
            with write_lock:
                logger.info('[#%04d] place=%s', line_no, result)
            stats.record_completed(line_no, 1)
            logger.info('[#%04d] COMPLETED  %s  entries=1', line_no, url)
            return

        error = scraper.sort_by(url, ind[args.sort_by])
        if error != 0:
            logger.warning('[#%04d] SKIPPED   %s  reason=sort_by_failed', line_no, url)
            stats.record_skipped(line_no)
            return

        n = 0
        while n < args.N:
            reviews = scraper.get_reviews(n)
            if not reviews:
                break

            with write_lock:
                for r in reviews:
                    row_data = list(r.values())
                    if args.source:
                        row_data.append(url)
                    writer.writerow(row_data)
                # Flush after every batch — if we crash after this point,
                # all rows written so far are already on disk.
                file_handle.flush()

            logger.debug('[#%04d] batch  offset=%d  size=%d', line_no, n, len(reviews))
            n += len(reviews)

        stats.record_completed(line_no, n)
        logger.info('[#%04d] COMPLETED  %s  reviews=%d', line_no, url, n)

    except Exception as exc:
        # One-line summary at WARNING so it's visible on console
        logger.warning('[#%04d] FAILED    %s  error=%r', line_no, url, exc)
        # Full traceback at DEBUG — present in the log file, not on console
        logger.debug('[#%04d] TRACEBACK:\n%s', line_no, tb_module.format_exc())
        stats.record_failed(line_no)


def _scrape_url_isolated(url, line_no, args, writer, file_handle, write_lock, stats, logger):
    """
    Spin up a dedicated Chrome instance for this URL, scrape, then tear it down.
    Used when workers > 1 so each thread is fully independent.
    """
    try:
        with GoogleMapsScraper(debug=args.debug, turbo=args.turbo) as scraper:
            _scrape_url(url, line_no, scraper, args, writer, file_handle,
                        write_lock, stats, logger)
    except Exception as exc:
        # Catches browser-init failures that happen before _scrape_url runs
        logger.warning('[#%04d] FAILED    %s  error=%r (browser init)', line_no, url, exc)
        logger.debug('[#%04d] TRACEBACK:\n%s', line_no, tb_module.format_exc())
        stats.record_failed(line_no)


# ── End-of-run summary ────────────────────────────────────────────────────────

def log_summary(logger, total_entries, stats, start_time, log_filename):
    """
    Print and log a structured summary that makes it unambiguous:
      - How many entries completed / failed / skipped.
      - Exactly which input-file line numbers failed or were skipped,
        so a user can extract and re-run only those entries.
      - The last successfully completed line (useful for simple sequential resume).
      - Total reviews written, elapsed time, and the log file path.
    """
    elapsed = datetime.now() - start_time
    # Strip microseconds for readability
    elapsed_str = str(elapsed).split('.')[0]
    sep = '─' * 66

    logger.info(sep)
    logger.info('RUN SUMMARY')
    logger.info('  Input entries  total    : %d', total_entries)
    logger.info('  Completed               : %d', stats.completed)
    logger.info('  Failed                  : %d', stats.failed)
    logger.info('  Skipped                 : %d', stats.skipped)
    logger.info('  Total reviews written   : %d', stats.total_reviews)
    logger.info('  Last completed line (#) : %s', stats.last_completed_line or 'none')
    logger.info('  Elapsed time            : %s', elapsed_str)
    logger.info('  Log file                : %s', log_filename)

    if stats.failed_lines:
        sorted_f = sorted(stats.failed_lines)
        logger.info('  FAILED  line(s) (#)     : %s', ', '.join(str(l) for l in sorted_f))

    if stats.skipped_lines:
        sorted_s = sorted(stats.skipped_lines)
        logger.info('  SKIPPED line(s) (#)     : %s', ', '.join(str(l) for l in sorted_s))

    problem_lines = sorted(set(stats.failed_lines + stats.skipped_lines))
    if problem_lines:
        logger.info('')
        logger.info('  RESUME HINT: %d line(s) need attention.', len(problem_lines))
        logger.info('  Extract those lines from the input file and re-run:')
        logger.info('    grep -n "" %s | grep -E "^(%s):" | cut -d: -f2- > retry.txt',
                    args_ref.i,
                    '|'.join(str(l) for l in problem_lines))
        logger.info('    python3 scraper.py --i retry.txt ...')
    else:
        logger.info('  All entries completed successfully.')

    logger.info(sep)


# ── Entry point ───────────────────────────────────────────────────────────────

# Module-level reference to parsed args — used only by log_summary for the hint
args_ref = None

if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Google Maps reviews scraper.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Execution modes
---------------
  Default (Mode A)  No extra flags. Single browser, safe rate of requests.
                    Add --workers 2 or --workers 3 for moderate parallelism.

  Turbo   (Mode B)  --turbo  Enables aggressive parallelism (default 4 workers)
                    plus browser-level optimisations (images disabled, eager
                    page-load, extra Chrome perf flags).  Accepts higher risk
                    of rate-limiting for maximum throughput.

Fault tolerance
---------------
  Output is written and flushed to disk after every scroll batch.  If the
  process is killed, all reviews extracted up to that moment are already in
  the CSV.  Re-run with the same --o flag (append mode — no duplicate header)
  or use the RESUME HINT printed at the end of each run to re-run only the
  failed/skipped entries.

Examples
--------
  # Single place, 50 reviews, newest first
  python3 scraper.py --N 50 --i urls.txt --o out.csv --sort_by newest

  # Bulk run, mode A, 2 workers
  python3 scraper.py --N 200 --i urls.txt --o out.csv --workers 2

  # Bulk run, mode B (turbo), 6 workers
  python3 scraper.py --N 200 --i urls.txt --o out.csv --turbo --workers 6

  # Place metadata only
  python3 scraper.py --place --i urls.txt

  # Custom log file
  python3 scraper.py --N 100 --i urls.txt --o out.csv --log my_run.log
""")

    parser.add_argument('--N', type=int, default=100,
                        help='Maximum number of reviews to retrieve per URL (default: 100)')
    parser.add_argument('--i', type=str, default='urls.txt',
                        help='Input file with target URLs, one per line (default: urls.txt)')
    parser.add_argument('--o', type=str, default='output.csv',
                        help='Output CSV filename, written to data/ (default: output.csv)')
    parser.add_argument('--sort_by', type=str, default='newest',
                        help='Sort order: most_relevant | newest | highest_rating | lowest_rating')
    parser.add_argument('--place', dest='place', action='store_true',
                        help='Extract place metadata instead of reviews')
    parser.add_argument('--debug', dest='debug', action='store_true',
                        help='Run with visible browser window (disables headless mode)')
    parser.add_argument('--source', dest='source', action='store_true',
                        help='Append source URL column to each row')
    parser.add_argument('--workers', type=int, default=None,
                        help=('Number of parallel browser workers. '
                              'Default: 1 (normal mode) or 4 (with --turbo). '
                              'Each worker runs its own Chrome instance.'))
    parser.add_argument('--turbo', dest='turbo', action='store_true',
                        help=('Mode B — aggressive parallelisation + browser optimisations. '
                              'Disables images, uses eager page-load strategy, raises default '
                              'worker count to 4. Accepts higher rate-limit risk.'))
    parser.add_argument('--log', type=str, default=None,
                        help=('Log filename. Default: scraper_YYYYMMDD_HHMMSS.log '
                              'in the current directory.'))

    parser.set_defaults(place=False, debug=False, source=False, turbo=False)

    args = parser.parse_args()
    args_ref = args     # make visible to log_summary

    if args.sort_by not in ind:
        print(f"Unknown sort_by value '{args.sort_by}'. Choose from: {list(ind.keys())}")
        raise SystemExit(1)

    # Resolve worker count: explicit --workers wins; otherwise mode-based default
    if args.workers is None:
        args.workers = DEFAULT_WORKERS_TURBO if args.turbo else DEFAULT_WORKERS_NORMAL

    if args.workers < 1:
        print('--workers must be >= 1')
        raise SystemExit(1)

    # ── Logging ───────────────────────────────────────────────────────────────
    log_filename = args.log or f'scraper_{datetime.now().strftime("%Y%m%d_%H%M%S")}.log'
    logger = setup_logger(log_filename)
    start_time = datetime.now()

    mode_label = 'turbo' if args.turbo else 'normal'
    logger.info('=' * 66)
    logger.info('Google Maps Reviews Scraper  started=%s',
                start_time.strftime('%Y-%m-%d %H:%M:%S'))
    logger.info('  mode=%-8s  workers=%d  N=%d  sort_by=%s',
                mode_label, args.workers, args.N, args.sort_by)
    logger.info('  input=%-30s  output=data/%s', args.i, args.o)
    logger.info('=' * 66)

    # ── Input ─────────────────────────────────────────────────────────────────
    # Preserve actual file line numbers (1-indexed) for log and resume hints.
    with open(args.i, 'r') as f:
        indexed_urls = [
            (i + 1, line.strip())
            for i, line in enumerate(f)
            if line.strip()
        ]

    total_entries = len(indexed_urls)
    logger.info('Loaded %d URL(s) from %s', total_entries, args.i)

    if not total_entries:
        logger.warning('Input file is empty — nothing to do.')
        raise SystemExit(0)

    # ── Output (append mode, crash-safe) ──────────────────────────────────────
    writer, file_handle = open_output_csv(args.source, args.o)
    write_lock = threading.Lock()
    stats      = RunStats()

    # ── Execution ─────────────────────────────────────────────────────────────
    try:
        if args.workers == 1:
            # ── Mode A / single-threaded ──────────────────────────────────────
            # Reuse one browser across all URLs — no per-URL startup overhead.
            with GoogleMapsScraper(debug=args.debug, turbo=args.turbo) as scraper:
                for line_no, url in indexed_urls:
                    _scrape_url(url, line_no, scraper, args,
                                writer, file_handle, write_lock, stats, logger)

        else:
            # ── Parallel mode (Mode A workers>1, or Mode B turbo) ─────────────
            # Each thread creates and destroys its own Chrome instance.
            # Thread-safe CSV writes are serialised through write_lock.
            with ThreadPoolExecutor(max_workers=args.workers) as executor:
                futures = {
                    executor.submit(
                        _scrape_url_isolated,
                        url, line_no, args,
                        writer, file_handle, write_lock, stats, logger
                    ): (line_no, url)
                    for line_no, url in indexed_urls
                }
                for fut in as_completed(futures):
                    line_no, url = futures[fut]
                    try:
                        fut.result()
                    except Exception as exc:
                        # Should never reach here — both worker functions catch
                        # everything internally — but kept as a hard safety net.
                        logger.error('[#%04d] UNHANDLED  %s  error=%r', line_no, url, exc)

    finally:
        # Guarantee the file is fully flushed and closed even on Ctrl-C or crash
        file_handle.flush()
        file_handle.close()
        log_summary(logger, total_entries, stats, start_time, log_filename)
