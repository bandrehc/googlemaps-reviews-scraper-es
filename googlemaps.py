# -*- coding: utf-8 -*-
import itertools
import logging
import re
import time
import traceback
from datetime import datetime, timedelta

import pandas as pd
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver import ChromeOptions as Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ── Constants ────────────────────────────────────────────────────────────────
GM_WEBPAGE = 'https://www.google.com/maps/'
MAX_WAIT   = 10
MAX_RETRY  = 5
MAX_SCROLLS = 40

# Selectors — update here if Google changes the DOM
REVIEWS_TAB_XPATH   = '//button[@role="tab" and (contains(text(),"Opiniones") or contains(@aria-label,"Revis"))]'
SORT_BUTTON_XPATH   = '//button[contains(@aria-label,"Ordenar")]'
SORT_OPTION_XPATH   = '//div[@role="menuitemradio"]'
REVIEW_BLOCK_CSS    = 'div.jftiEf'
REVIEW_TEXT_CSS     = 'span.wiI7pd'
RATING_SPAN_CSS     = 'span.kvMYJc'
DATE_SPAN_CSS       = 'span.rsqaWe'
NREVIEWS_DIV_CSS    = 'div.RfnDt'
USER_BUTTON_CSS     = 'button.WEBjve'
EXPAND_BUTTON_CSS   = 'button.w8nwRe.kyuRq'
SCROLL_DIV_CSS      = 'div.m6QErb.DxyBCb.kA9KIf.dS8AEf'
PLACE_NAME_CSS      = 'h1.DUwDvf'

# User-agent for anti-bot headers
UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36')

# ── Timing constants ──────────────────────────────────────────────────────────
# Normal mode: conservative waits — reliable across slow connections
AJAX_TIMEOUT_NORMAL    = 8    # max seconds to wait for new reviews after a scroll
SORT_RELOAD_TIMEOUT    = 12   # max seconds to wait for reviews to reload after sort change
ACCOUNT_WAIT_TIMEOUT   = 10   # max seconds to wait for place h1 to appear
RETRY_SLEEP_NORMAL     = 1.0  # sleep between sort-button retry attempts
SORT_FALLBACK_SLEEP    = 2.0  # fallback sleep if dynamic sort-reload wait fails
PLACES_FALLBACK_SLEEP  = 1.0  # fallback sleep per search point in get_places()

# Turbo mode: tighter waits — faster but may miss slow-loading content
AJAX_TIMEOUT_TURBO     = 5
SORT_RELOAD_TIMEOUT_TURBO  = 8
ACCOUNT_WAIT_TIMEOUT_TURBO = 6
RETRY_SLEEP_TURBO      = 0.5
SORT_FALLBACK_SLEEP_TURBO  = 1.0
PLACES_FALLBACK_SLEEP_TURBO = 0.3
# ─────────────────────────────────────────────────────────────────────────────


class GoogleMapsScraper:

    def __init__(self, debug=False, turbo=False):
        self.debug  = debug
        self.turbo  = turbo

        # Resolve per-instance timing based on mode
        self._ajax_timeout         = AJAX_TIMEOUT_TURBO    if turbo else AJAX_TIMEOUT_NORMAL
        self._sort_reload_timeout  = SORT_RELOAD_TIMEOUT_TURBO if turbo else SORT_RELOAD_TIMEOUT
        self._account_wait_timeout = ACCOUNT_WAIT_TIMEOUT_TURBO if turbo else ACCOUNT_WAIT_TIMEOUT
        self._retry_sleep          = RETRY_SLEEP_TURBO     if turbo else RETRY_SLEEP_NORMAL
        self._sort_fallback_sleep  = SORT_FALLBACK_SLEEP_TURBO if turbo else SORT_FALLBACK_SLEEP
        self._places_fallback_sleep = PLACES_FALLBACK_SLEEP_TURBO if turbo else PLACES_FALLBACK_SLEEP

        # Track seen review IDs to avoid re-processing duplicates across scroll batches
        self._seen_ids = set()

        self.driver = self.__get_driver()
        self.logger = self.__get_logger()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, tb):
        if exc_type is not None:
            traceback.print_exception(exc_type, exc_value, tb)
        self.driver.close()
        self.driver.quit()
        return True

    # ── Public API ────────────────────────────────────────────────────────────

    def sort_by(self, url, ind):
        # Reset seen-ID tracking for this URL
        self._seen_ids.clear()

        self.driver.get(url.strip())
        self.__click_on_cookie_agreement()

        wait = WebDriverWait(self.driver, MAX_WAIT)

        # Navigate to the reviews tab — it must be active before sort appears
        if not self.__open_reviews_tab(wait):
            self.logger.error('Reviews tab not found for: %s', url.strip())
            return -1

        # Click the sort dropdown
        clicked = False
        tries   = 0
        while not clicked and tries < MAX_RETRY:
            try:
                menu_bt = wait.until(EC.element_to_be_clickable((By.XPATH, SORT_BUTTON_XPATH)))
                menu_bt.click()
                clicked = True
            except Exception as e:
                tries += 1
                self.logger.warning('Sort button not found (attempt %d/%d): %s', tries, MAX_RETRY, e)
                time.sleep(self._retry_sleep)

        if not clicked:
            self.logger.error('Could not click sort button after %d attempts for: %s', MAX_RETRY, url.strip())
            return -1

        # Wait for sort option items to become visible
        try:
            WebDriverWait(self.driver, MAX_WAIT).until(
                EC.visibility_of_element_located((By.XPATH, SORT_OPTION_XPATH)))
        except TimeoutException:
            self.logger.warning('Sort options did not become visible; proceeding anyway')

        # Select the desired sort option by position index
        options = self.driver.find_elements(By.XPATH, SORT_OPTION_XPATH)
        if not options:
            self.logger.error('Sort menu options not found after opening dropdown')
            return -1
        if ind >= len(options):
            self.logger.error('Sort index %d out of range — only %d options found', ind, len(options))
            return -1

        options[ind].click()

        # Wait for reviews to reload with the new sort order
        self.__wait_for_sort_reload()
        return 0

    def get_reviews(self, offset):
        # Count reviews currently in DOM before scrolling
        before_count = len(self.driver.find_elements(By.CSS_SELECTOR, REVIEW_BLOCK_CSS))

        # Scroll to trigger lazy-loading of the next batch
        self.__scroll()

        # Dynamic wait: block until new review cards appear (or timeout)
        try:
            WebDriverWait(self.driver, self._ajax_timeout).until(
                lambda d: len(d.find_elements(By.CSS_SELECTOR, REVIEW_BLOCK_CSS)) > before_count
            )
        except TimeoutException:
            # No new reviews loaded — either end of list or very slow network
            pass

        # Expand truncated review text
        self.__expand_reviews()

        response = BeautifulSoup(self.driver.page_source, 'html.parser')
        rblock = response.find_all('div', class_='jftiEf fontBodyMedium')
        if not rblock:
            self.logger.warning('No review blocks found (class jftiEf fontBodyMedium)')

        parsed_reviews = []
        for index, review in enumerate(rblock):
            # Skip already-processed reviews (by position offset)
            if index < offset:
                continue

            r = self.__parse(review)

            # Dedup by review ID across multiple get_reviews() calls for the same URL
            rid = r.get('id_review')
            if rid:
                if rid in self._seen_ids:
                    continue
                self._seen_ids.add(rid)

            parsed_reviews.append(r)
            print(r)

        return parsed_reviews

    # Needs a different URL than the reviews URL to get all place info
    def get_account(self, url):
        self.driver.get(url.strip())
        self.__click_on_cookie_agreement()

        # Wait for the place name heading — more reliable than a fixed sleep
        try:
            WebDriverWait(self.driver, self._account_wait_timeout).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, PLACE_NAME_CSS)))
        except TimeoutException:
            pass  # proceed with whatever has loaded

        resp = BeautifulSoup(self.driver.page_source, 'html.parser')
        return self.__parse_place(resp, url.strip())

    def get_places(self, keyword_list=None):
        keyword_list = [] if keyword_list is None else keyword_list
        df_places = pd.DataFrame()
        search_point_url_list = self._gen_search_points_from_square(keyword_list=keyword_list)

        for i, search_point_url in enumerate(search_point_url_list):
            print(search_point_url)

            if (i + 1) % 10 == 0:
                print(f"{i}/{len(search_point_url_list)}")
                out = df_places[['search_point_url', 'href', 'name', 'rating', 'num_reviews', 'close_time', 'other']]
                out.to_csv('output/places_wax.csv', index=False)

            try:
                self.driver.get(search_point_url)
            except NoSuchElementException:
                self.driver.quit()
                self.driver = self.__get_driver()
                self.driver.get(search_point_url)

            # Scroll to load all 20 places on the page
            scrollable_div = self.driver.find_element(By.CSS_SELECTOR,
                "div.m6QErb.DxyBCb.kA9KIf.dS8AEf.ecceSd > div[aria-label*='Resultados para']")
            for _ in range(10):
                self.driver.execute_script('arguments[0].scrollTop = arguments[0].scrollHeight', scrollable_div)

            # Dynamic wait for place links — fallback to short sleep
            try:
                WebDriverWait(self.driver, self._ajax_timeout).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, 'div[jsaction] > a[href]')))
            except TimeoutException:
                time.sleep(self._places_fallback_sleep)

            response = BeautifulSoup(self.driver.page_source, 'html.parser')
            div_places = response.select('div[jsaction] > a[href]')

            rows = []
            for div_place in div_places:
                rows.append({
                    'search_point_url': search_point_url.replace('https://www.google.com/maps/search/', ''),
                    'href': div_place['href'],
                    'name': div_place['aria-label'],
                })
            if rows:
                df_places = pd.concat([df_places, pd.DataFrame(rows)], ignore_index=True)

        df_places = df_places[['search_point_url', 'href', 'name']]
        df_places.to_csv('output/places_wax.csv', index=False)

    # ── Private helpers ───────────────────────────────────────────────────────

    def __open_reviews_tab(self, wait):
        """Click the Reviews/Opiniones tab and wait for the sort button to be present."""
        try:
            tab = wait.until(EC.element_to_be_clickable((By.XPATH, REVIEWS_TAB_XPATH)))
            self.logger.info('Clicking reviews tab: %s', tab.text)
            tab.click()
            # Wait for the sort button to appear — proof that the reviews panel is active
            WebDriverWait(self.driver, MAX_WAIT).until(
                EC.presence_of_element_located((By.XPATH, SORT_BUTTON_XPATH)))
            return True
        except Exception as e:
            self.logger.warning('Reviews tab not clickable or sort button not found: %s', e)
            return False

    def __wait_for_sort_reload(self):
        """
        Wait for the reviews list to reload after a sort-option click.

        Strategy: capture a reference to the first review block, click, then wait for
        that element to go stale (page re-rendered) and for a fresh block to appear.
        Falls back to a short fixed sleep when no review blocks are present yet.
        """
        sort_wait = WebDriverWait(self.driver, self._sort_reload_timeout)
        try:
            first_review = self.driver.find_element(By.CSS_SELECTOR, REVIEW_BLOCK_CSS)
            # Wait for the DOM node to become stale (Google replaced it)
            sort_wait.until(EC.staleness_of(first_review))
            # Wait for the freshly-sorted reviews to appear
            sort_wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, REVIEW_BLOCK_CSS)))
        except (NoSuchElementException, TimeoutException):
            # No review blocks yet, or staleness check timed out — use fallback
            time.sleep(self._sort_fallback_sleep)

    def __parse(self, review):
        item = {}
        retrieval_date = datetime.now()

        try:
            id_review = review['data-review-id']
        except Exception:
            id_review = None

        try:
            username = review['aria-label']
        except Exception:
            username = None

        try:
            review_text = self.__filter_string(review.find('span', class_='wiI7pd').text)
        except Exception:
            review_text = None

        try:
            aria_label = review.find('span', class_='kvMYJc')['aria-label']
            m = re.search(r'(\d+)', aria_label)
            rating = float(m.group(1)) if m else None
        except Exception:
            rating = None

        try:
            relative_date = review.find('span', class_='rsqaWe').text
        except Exception:
            relative_date = None

        try:
            n_reviews = review.find('div', class_='RfnDt').text.split(' ')[3]
        except Exception:
            n_reviews = 0

        try:
            user_url = review.find('button', class_='WEBjve')['data-href']
        except Exception:
            user_url = None

        item['id_review']      = id_review
        item['caption']        = review_text
        item['relative_date']  = relative_date
        item['review_date']    = self.__calculate_review_date(relative_date, retrieval_date)
        item['retrieval_date'] = retrieval_date
        item['rating']         = rating
        item['username']       = username
        item['n_review_user']  = n_reviews
        item['n_photo_user']   = None   # kept for schema compatibility; no longer available
        item['url_user']       = user_url

        return item

    def __calculate_review_date(self, relative_date_str, retrieval_date):
        """Approximate review date by subtracting the relative duration from retrieval_date."""
        if not relative_date_str:
            return retrieval_date
        try:
            s  = relative_date_str.replace('Editado ', '').strip()
            m  = re.search(r'(\d+)', s)
            if m:
                value = int(m.group(1))
            elif re.search(r'\bun[ao]?\b', s, re.I):
                value = 1
            else:
                return retrieval_date

            sl = s.lower()
            if   'segundo' in sl: return retrieval_date - timedelta(seconds=value)
            elif 'minuto'  in sl: return retrieval_date - timedelta(minutes=value)
            elif 'hora'    in sl: return retrieval_date - timedelta(hours=value)
            elif 'día'     in sl: return retrieval_date - timedelta(days=value)
            elif 'semana'  in sl: return retrieval_date - timedelta(weeks=value)
            elif 'mes'     in sl: return retrieval_date - timedelta(days=value * 30)
            elif 'año'     in sl: return retrieval_date - timedelta(days=value * 365)
            else:
                return retrieval_date
        except (ValueError, IndexError):
            return retrieval_date

    def __parse_place(self, response, url):
        place = {}

        try:
            place['name'] = response.find('h1', class_='DUwDvf fontHeadlineLarge').text.strip()
        except Exception:
            place['name'] = None

        try:
            place['overall_rating'] = float(
                response.find('div', class_='F7nice ').find('span', class_='ceNzKf')['aria-label'].split(' ')[1])
        except Exception:
            place['overall_rating'] = None

        try:
            place['n_reviews'] = int(
                response.find('div', class_='F7nice ').text.split('(')[1].replace(',', '').replace(')', ''))
        except Exception:
            place['n_reviews'] = 0

        try:
            place['n_photos'] = int(
                response.find('div', class_='YkuOqf').text.replace('.', '').replace(',', '').split(' ')[0])
        except Exception:
            place['n_photos'] = 0

        try:
            place['category'] = response.find('button', jsaction='pane.rating.category').text.strip()
        except Exception:
            place['category'] = None

        try:
            place['description'] = response.find('div', class_='PYvSYb').text.strip()
        except Exception:
            place['description'] = None

        b_list = response.find_all('div', class_='Io6YTe fontBodyMedium')
        for key, idx in [('address', 0), ('website', 1), ('phone_number', 2), ('plus_code', 3)]:
            try:
                place[key] = b_list[idx].text
            except Exception:
                place[key] = None

        try:
            place['opening_hours'] = response.find('div', class_='t39EBf GUrTXd')['aria-label'].replace('\u202f', ' ')
        except Exception:
            place['opening_hours'] = None

        place['url'] = url

        try:
            lat, long, _ = url.split('/')[6].split(',')
            place['lat']  = lat[1:]
            place['long'] = long
        except Exception:
            place['lat']  = None
            place['long'] = None

        return place

    def _gen_search_points_from_square(self, keyword_list=None):
        keyword_list = [] if keyword_list is None else keyword_list
        square_points = pd.read_csv('input/square_points.csv')
        cities = square_points['city'].unique()
        search_urls = []
        for city in cities:
            df_aux = square_points[square_points['city'] == city]
            latitudes  = df_aux['latitude'].unique()
            longitudes = df_aux['longitude'].unique()
            coordinates_list = list(itertools.product(latitudes, longitudes, keyword_list))
            search_urls += [
                f"https://www.google.com/maps/search/{c[2]}/@{c[1]},{c[0]},15z"
                for c in coordinates_list
            ]
        return search_urls

    def __expand_reviews(self):
        """Click 'Más' buttons to expand truncated review text."""
        buttons = self.driver.find_elements(By.CSS_SELECTOR, EXPAND_BUTTON_CSS)
        if not buttons:
            self.logger.debug('No expand buttons found')
        for button in buttons:
            self.driver.execute_script("arguments[0].click();", button)

    def __scroll(self):
        """Scroll the reviews panel to trigger lazy-loading."""
        try:
            scrollable_div = self.driver.find_element(By.CSS_SELECTOR, SCROLL_DIV_CSS)
            self.driver.execute_script('arguments[0].scrollTop = arguments[0].scrollHeight', scrollable_div)
        except NoSuchElementException:
            self.logger.warning('Scrollable reviews div not found (%s)', SCROLL_DIV_CSS)

    def __get_logger(self):
        logger = logging.getLogger(f'googlemaps-scraper-{id(self)}')
        logger.setLevel(logging.DEBUG)
        fh = logging.FileHandler('gm-scraper.log')
        fh.setLevel(logging.DEBUG)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        logger.addHandler(fh)
        return logger

    def __get_driver(self):
        options = Options()

        if not self.debug:
            options.add_argument('--headless=new')
        else:
            options.add_argument('--window-size=1366,768')

        options.add_argument('--disable-notifications')
        options.add_argument('--accept-lang=es')
        options.add_argument('--no-sandbox')
        options.add_argument('--disable-dev-shm-usage')

        # Prevent Google from detecting automated/headless Chrome
        options.add_argument('--disable-blink-features=AutomationControlled')
        options.add_experimental_option('excludeSwitches', ['enable-automation'])
        options.add_experimental_option('useAutomationExtension', False)
        options.add_argument(f'--user-agent={UA}')

        if self.turbo:
            # Block image loading — saves significant bandwidth and CPU per page
            prefs = {
                'profile.managed_default_content_settings.images': 2,
                'profile.default_content_settings.images': 2,
            }
            options.add_experimental_option('prefs', prefs)
            # Eager strategy: don't wait for images/fonts — return as soon as DOM is ready
            options.page_load_strategy = 'eager'
            options.add_argument('--disable-extensions')
            options.add_argument('--disable-gpu')
            options.add_argument('--disable-software-rasterizer')
            options.add_argument('--disable-default-apps')
            options.add_argument('--no-first-run')

        driver = webdriver.Chrome(service=Service(), options=options)

        # Remove the `navigator.webdriver` flag at runtime
        driver.execute_cdp_cmd('Network.setUserAgentOverride', {'userAgent': UA})
        driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        driver.get(GM_WEBPAGE)
        return driver

    def __click_on_cookie_agreement(self):
        try:
            agree = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, '//span[contains(text(), "Rechazar todo")]')))
            agree.click()
            return True
        except Exception:
            return False

    def __filter_string(self, s):
        return s.replace('\r', ' ').replace('\n', ' ').replace('\t', ' ')
