"""
Weekly Keepa pull — integrated version.

Combines five independently-tested pieces:
  1. Google Sheets connection
  2. Keepa single-ASIN pull + parsing
  3. Batch loop over asins.txt
  4. Sheet-writing logic (new dated column per run, matched to ASIN rows)
  5. Token balance check (pre-run only, at this stage)

Skip-queue prioritization is NOT included yet — that's a planned refinement
once this base version has run reliably for a few weeks.

Before running:
1. .env file with:
     KEEPA=your_keepa_api_key
     SHEET_ID=your_google_sheet_id
2. credentials.json (Google service account key) in the same folder.
3. asins.txt in the same folder, one ASIN per line.
4. pip install requests gspread google-auth python-dotenv
"""

import os
import time
import json
from datetime import datetime

from dotenv import load_dotenv
import requests
import gspread
from google.oauth2.service_account import Credentials

load_dotenv()

# ---- Config ----
API_KEY = os.getenv('KEEPA')
SHEET_ID = os.getenv('SHEET_ID')
CREDENTIALS_FILE = 'credentials.json'
ASIN_FILE = 'asins.txt'

DATE_STR = datetime.today().strftime('%m/%d/%Y')
DELAY_SECONDS = 13  # ~1 token/13 sec = 5/min, safe for 1 token per product

RATING_SHEET_NAME = 'Star Ratings'
REVIEW_SHEET_NAME = 'Review Counts'

STAGGER_SHEET_ID = os.getenv('STAGGER_SHEET_ID')
MAIN_TAB_NAME = 'Main'
REFERENCE_HEADERS = ['FBA SKU', 'Product', 'Order Status', 'Grade']

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


# ---- Component 1: Google Sheets connection ----
def get_gspread_client():
    """Authenticate using GOOGLE_CREDS_JSON (GitHub Actions secret) if present,
    otherwise fall back to a local credentials.json file for local testing."""
    creds_json = os.getenv('GOOGLE_CREDS_JSON')
    if creds_json:
        creds_dict = json.loads(creds_json)
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


# ---- Component 2: Keepa single-ASIN pull + parsing ----
def parse_product(p):
    csv = p.get('csv') or []

    rating_arr = csv[16] if len(csv) > 16 and csv[16] else []
    star_rating = ''
    if rating_arr:
        raw = rating_arr[-1]
        star_rating = round(raw / 10, 1) if raw > 0 else ''

    review_arr = csv[17] if len(csv) > 17 and csv[17] else []
    review_count = ''
    if review_arr:
        raw = review_arr[-1]
        review_count = raw if raw > 0 else ''

    return star_rating, review_count


def load_asins(filepath):
    with open(filepath, 'r') as f:
        return [line.strip() for line in f if line.strip()]


# ---- Component 5: Token balance check ----
def check_token_status():
    """Query Keepa's token endpoint. Does NOT consume a data token."""
    try:
        url = f'https://api.keepa.com/token?key={API_KEY}'
        response = requests.get(url, timeout=15)
        if response.status_code != 200:
            print(f'Could not check token balance: HTTP {response.status_code}')
            return None, None, None
        data = response.json()
        return data.get('tokensLeft'), data.get('refillIn'), data.get('refillRate')
    except Exception as e:
        print(f'Could not check token balance: {e}')
        return None, None, None


# ---- Component 3: Batch loop over ASINs ----
def fetch_keepa_data(asins):
    """Pull rating + review count for each ASIN. Returns dict asin -> (rating, review_count).
    Checks token balance before starting and trims the list if there isn't
    enough to cover every ASIN, so the run finishes cleanly rather than
    erroring out partway through."""

    tokens_left, refill_in_ms, refill_rate = check_token_status()
    if tokens_left is not None:
        print(f'Keepa tokens available: {tokens_left}')
        if tokens_left < len(asins):
            refill_min = round(refill_in_ms / 60000, 1) if refill_in_ms else '?'
            print(
                f'WARNING: Only {tokens_left} tokens available but {len(asins)} ASINs are queued '
                f'(refill rate ~{refill_rate}/min, next refill in ~{refill_min} min). '
                f'Processing the first {tokens_left} ASINs this run — the rest will simply '
                f'be missing from this week\'s column.'
            )
            asins = asins[:tokens_left]
    else:
        print('Skipping token pre-check (endpoint unreachable) — proceeding normally.')

    results = {}
    for i, asin in enumerate(asins):
        print(f'[{i + 1}/{len(asins)}] Pulling {asin}...', end=' ')

        try:
            url = f'https://api.keepa.com/product?key={API_KEY}&domain=1&asin={asin}&rating=1'
            response = requests.get(url, timeout=30)

            if response.status_code != 200:
                print(f'HTTP {response.status_code} — {response.text[:150]}')
                results[asin] = ('ERROR', 'ERROR')
            else:
                data = response.json()
                products = data.get('products')
                if not products:
                    print('No data returned')
                    results[asin] = ('', '')
                else:
                    rating, reviews = parse_product(products[0])
                    results[asin] = (rating, reviews)
                    print(f'Done — rating={rating}, reviews={reviews}')

        except Exception as e:
            print(f'Error: {e}')
            results[asin] = ('ERROR', 'ERROR')

        if i < len(asins) - 1:
            time.sleep(DELAY_SECONDS)

    return results


# ---- Stagger Charts reference lookup ----
def build_stagger_lookup(gc):
    """Read the Main tab of Stagger Charts V9 and build a dict:
    ASIN -> (FBA SKU, Product, Order Status, Grade)."""
    stagger_sheet = gc.open_by_key(STAGGER_SHEET_ID)
    ws = stagger_sheet.worksheet(MAIN_TAB_NAME)
    all_values = ws.get_all_values()

    lookup = {}
    for row in all_values[1:]:
        if len(row) <= 11:
            continue
        asin = row[11].strip()
        if not asin:
            continue
        fba_sku = row[1] if len(row) > 1 else ''
        product = row[2] if len(row) > 2 else ''
        order_status = row[6] if len(row) > 6 else ''
        grade = row[14] if len(row) > 14 else ''
        lookup[asin] = (fba_sku, product, order_status, grade)

    return lookup


def refresh_reference_columns(ws, lookup):
    """Overwrite columns B-E for every ASIN row with the latest values from
    Stagger Charts, so this data never goes stale relative to the master sheet."""
    asin_col = ws.col_values(1)  # index 0 is the header
    rows = []
    for asin in asin_col[1:]:
        values = lookup.get(asin, ('', '', '', ''))
        rows.append(list(values))

    if rows:
        ws.update('B2', rows)


# ---- Component 4: Sheet-writing logic ----
def col_letter(n):
    """Convert a 1-indexed column number to its A1 letter (1 -> A, 27 -> AA, etc.)."""
    letters = ''
    while n > 0:
        n, remainder = divmod(n - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def get_or_create_worksheet(sheet, name, asins):
    """Get the worksheet by name, creating it (and seeding column A with ASINs,
    plus the reference column headers) if needed. If it already exists,
    appends any ASINs not yet present."""
    try:
        ws = sheet.worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title=name, rows=len(asins) + 10, cols=52)
        header = ['ASIN'] + REFERENCE_HEADERS
        ws.update('A1', [header] + [[a] for a in asins])
        return ws

    existing = ws.col_values(1)[1:]  # skip header row
    new_asins = [a for a in asins if a not in existing]
    if new_asins:
        start_row = len(existing) + 2
        ws.update(f'A{start_row}', [[a] for a in new_asins])

    return ws


def write_column(ws, values_by_asin, header):
    """Append a new column with `header` as the top cell, filled in ASIN row order."""
    asin_col = ws.col_values(1)  # index 0 is the header
    next_col_index = len(ws.row_values(1)) + 1
    letter = col_letter(next_col_index)

    col_values = [[header]]
    for asin in asin_col[1:]:
        col_values.append([values_by_asin.get(asin, '')])

    ws.update(f'{letter}1', col_values)


# ---- Integration ----
def main():
    asins = load_asins(ASIN_FILE)
    print(f'Loaded {len(asins)} ASINs\n')

    print('Connecting to Google Sheet...')
    gc = get_gspread_client()
    sheet = gc.open_by_key(SHEET_ID)
    print(f'Connected to "{sheet.title}"\n')

    rating_ws = get_or_create_worksheet(sheet, RATING_SHEET_NAME, asins)
    review_ws = get_or_create_worksheet(sheet, REVIEW_SHEET_NAME, asins)

    print('Refreshing reference columns (FBA SKU, Product, Order Status, Grade) from Stagger Charts...')
    stagger_lookup = build_stagger_lookup(gc)
    refresh_reference_columns(rating_ws, stagger_lookup)
    refresh_reference_columns(review_ws, stagger_lookup)
    print(f'  Matched {len(stagger_lookup)} ASINs available in Stagger Charts\n')

    results = fetch_keepa_data(asins)

    ratings = {asin: vals[0] for asin, vals in results.items()}
    reviews = {asin: vals[1] for asin, vals in results.items()}

    write_column(rating_ws, ratings, DATE_STR)
    write_column(review_ws, reviews, DATE_STR)

    print(f'\nComplete. "{RATING_SHEET_NAME}" and "{REVIEW_SHEET_NAME}" updated for {DATE_STR}')


if __name__ == '__main__':
    main()