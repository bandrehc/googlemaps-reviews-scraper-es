# -*- coding: utf-8 -*-

from googlemaps import GoogleMapsScraper
import argparse
import csv
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from termcolor import colored


ind = {'most_relevant': 0, 'newest': 1, 'highest_rating': 2, 'lowest_rating': 3}

HEADER = ['id_review', 'caption', 'relative_date', 'review_date', 'retrieval_date',
          'rating', 'username', 'n_review_user', 'n_photo_user', 'url_user']
HEADER_W_SOURCE = HEADER + ['url_source']

# Default worker counts per mode
DEFAULT_WORKERS_NORMAL = 1
DEFAULT_WORKERS_TURBO  = 4


def csv_writer(source_field, outpath):
    targetfile = open('data/' + outpath, mode='w', encoding='utf-8', newline='\n')
    writer = csv.writer(targetfile, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(HEADER_W_SOURCE if source_field else HEADER)
    return writer


def _scrape_url(url, scraper, args, writer, lock):
    """Process one URL with an existing scraper instance (shared or per-thread)."""
    if args.place:
        result = scraper.get_account(url)
        with lock:
            print(result)
    else:
        error = scraper.sort_by(url, ind[args.sort_by])

        if error == 0:
            n = 0
            while n < args.N:
                print(colored(f'[{url[:50]}] Review {n}', 'cyan'))
                reviews = scraper.get_reviews(n)
                if len(reviews) == 0:
                    break

                with lock:
                    for r in reviews:
                        row_data = list(r.values())
                        if args.source:
                            row_data.append(url)
                        writer.writerow(row_data)

                n += len(reviews)
        else:
            print(colored(f'[skip] sort_by failed for {url}', 'red'))


def _scrape_url_isolated(url, args, writer, lock):
    """
    Create a dedicated browser instance for this URL, scrape it, then close.
    Used in parallel (workers > 1) mode so each thread owns its own Chrome.
    """
    with GoogleMapsScraper(debug=args.debug, turbo=args.turbo) as scraper:
        _scrape_url(url, scraper, args, writer, lock)


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

    parser.set_defaults(place=False, debug=False, source=False, turbo=False)

    args = parser.parse_args()

    if args.sort_by not in ind:
        print(f"Unknown sort_by value '{args.sort_by}'. Choose from: {list(ind.keys())}")
        raise SystemExit(1)

    # Resolve worker count: explicit --workers wins; otherwise mode-based default
    if args.workers is None:
        args.workers = DEFAULT_WORKERS_TURBO if args.turbo else DEFAULT_WORKERS_NORMAL

    if args.workers < 1:
        print("--workers must be >= 1")
        raise SystemExit(1)

    writer = csv_writer(args.source, args.o)
    lock   = threading.Lock()

    with open(args.i, 'r') as urls_file:
        urls = [u.strip() for u in urls_file if u.strip()]

    if not urls:
        print("Input file is empty — nothing to do.")
        raise SystemExit(0)

    mode_label = f'turbo (workers={args.workers})' if args.turbo else f'normal (workers={args.workers})'
    print(colored(f'[scraper] mode={mode_label}  urls={len(urls)}  N={args.N}', 'yellow'))

    if args.workers == 1:
        # ── Mode A / single-threaded ──────────────────────────────────────────
        # Reuse one browser across all URLs — no startup overhead per URL.
        with GoogleMapsScraper(debug=args.debug, turbo=args.turbo) as scraper:
            for url in urls:
                _scrape_url(url, scraper, args, writer, lock)

    else:
        # ── Parallel mode (Mode A with workers>1, or Mode B / turbo) ─────────
        # Each thread owns its own Chrome instance; thread-safe CSV writes via lock.
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(_scrape_url_isolated, url, args, writer, lock): url
                for url in urls
            }
            for fut in as_completed(futures):
                url = futures[fut]
                try:
                    fut.result()
                except Exception as exc:
                    print(colored(f'[error] {url}: {exc}', 'red'))
