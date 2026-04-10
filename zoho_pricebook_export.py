#!/usr/bin/env python3
"""
zoho_pricebook_export.py — Export Zoho CRM Price Book products and list prices to CSV.

First run opens a browser for login. Your org and session are auto-detected
and cached in zohocrm_session.json next to the script. Subsequent runs reuse the
cached session until it expires.

Requirements:
    pip install requests
    pip install playwright && playwright install chromium  # for auto-login

Usage:
    python zoho_pricebook_export.py                          # interactive picker
    python zoho_pricebook_export.py --list                   # show all price books
    python zoho_pricebook_export.py --pricebook "My Book"    # export by name (partial match)
    python zoho_pricebook_export.py --pricebook 12345678...  # export by ID
    python zoho_pricebook_export.py --login                  # force re-login
    python zoho_pricebook_export.py --cookies '...'          # manual cookies (no Playwright needed)

Tested with: Python 3.10+, Zoho CRM (zoho.com, zoho.eu, zoho.in, zoho.com.au)

License: MIT
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.parse
from html.parser import HTMLParser

import requests

# ── Constants ──────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE = os.path.join(SCRIPT_DIR, "zohocrm_session.json")
PAGE_SIZE = 10  # Zoho forces 10 records per page in the Edit List Prices popup
OUTPUT_FILE = "pricebook_export.csv"

# Generic starting URL for first-time login. Zoho redirects to the correct
# regional domain (zoho.eu, zoho.in, etc.) based on the user's account.
GENERIC_LOGIN_URL = "https://crm.zoho.com/crm/"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)


# ── Session persistence ────────────────────────────────────────
#
# Everything lives in one file (zohocrm_session.json):
#   {
#     "org_id": "12345",
#     "domain": "zoho.com",
#     "rid": "16477...",          <-- cached after first detection
#     "cookies": { ... }          <-- session cookies
#   }

def _load_session() -> dict:
    """Read the session file, or return empty dict if missing/corrupt."""
    if not os.path.exists(SESSION_FILE):
        return {}
    try:
        with open(SESSION_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_session(data: dict) -> None:
    """Write the session file with restricted permissions (contains tokens)."""
    with open(SESSION_FILE, "w") as f:
        json.dump(data, f, indent=2)
    try:
        os.chmod(SESSION_FILE, 0o600)
    except OSError:
        pass  # Windows doesn't support Unix permissions


def load_config() -> dict | None:
    """Load org config (org_id, domain, rid) from the session file."""
    sess = _load_session()
    if "org_id" in sess and "domain" in sess:
        return {k: v for k, v in sess.items() if k != "cookies"}
    return None


def save_config(config: dict) -> None:
    """Merge org config into the session file (preserves cookies)."""
    sess = _load_session()
    sess.update(config)
    _save_session(sess)


def save_cookies(cookies: dict) -> None:
    """Store cookies in the session file (preserves config)."""
    sess = _load_session()
    sess["cookies"] = cookies
    _save_session(sess)


def load_cookies() -> dict | None:
    """Load cookies from the session file. Returns None if missing or no CSRF token."""
    sess = _load_session()
    cookies = sess.get("cookies")
    if cookies and _get_csrf(cookies):
        return cookies
    return None


# ── URL and header helpers ─────────────────────────────────────

def _crm_base(config: dict) -> str:
    """Base URL for Zoho CRM web endpoints, e.g. https://crm.zoho.com/crm/org12345"""
    return f"https://crm.{config['domain']}/crm/org{config['org_id']}"


def _api_base(config: dict) -> str:
    """Base URL for Zoho CRM internal API, e.g. https://crm.zoho.com/crm/v2.2"""
    return f"https://crm.{config['domain']}/crm/v2.2"


def _req_headers(config: dict) -> dict:
    """Standard headers for form-encoded POST requests to CRM web endpoints."""
    return {
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": f"https://crm.{config['domain']}",
        "Referer": f"https://crm.{config['domain']}/",
        "X-Requested-With": "XMLHttpRequest, XMLHttpRequest",
        "X-CRM-ORG": config["org_id"],
        "User-Agent": USER_AGENT,
    }


def _api_headers(config: dict, csrf: str) -> dict:
    """Headers for Zoho CRM internal JSON API (v2.2) requests."""
    boundary = "----WebKitFormBoundaryZohoExport"
    return {
        "Accept": "*/*",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Origin": f"https://crm.{config['domain']}",
        "Referer": f"https://crm.{config['domain']}/",
        "X-Requested-With": "XMLHttpRequest",
        "X-CRM-ORG": config["org_id"],
        "X-ZCSRF-TOKEN": f"crmcsrfparam={csrf}",
        "User-Agent": USER_AGENT,
    }


# Zoho's internal API expects an empty multipart body with a closing boundary.
API_BODY = "------WebKitFormBoundaryZohoExport--\r\n"


# ── Cookie utilities ───────────────────────────────────────────

def parse_cookie_string(raw: str) -> dict:
    """Parse a 'key=val; key2=val2' cookie string into a dict."""
    cookies = {}
    for item in raw.split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            cookies[k.strip()] = v.strip()
    return cookies


def _get_csrf(cookies: dict) -> str:
    """Extract the CSRF token from cookies (tried under two known names)."""
    return cookies.get("crmcsr", cookies.get("CSRF_TOKEN", ""))


# ── Config detection ───────────────────────────────────────────

def _extract_config_from_url(url: str) -> dict | None:
    """
    Extract org_id and Zoho domain from a CRM URL.
    Example: https://crm.zoho.com/crm/org12345678/tab/... -> {org_id: "12345678", domain: "zoho.com"}
    Supports regional domains: zoho.eu, zoho.in, zoho.com.au, etc.
    """
    m = re.search(r"crm\.(zoho\.\w+(?:\.\w+)?)/crm/org(\d+)", url)
    if m:
        return {"domain": m.group(1), "org_id": m.group(2)}
    return None


# ── Playwright login ───────────────────────────────────────────

def _login_with_playwright(config: dict | None = None) -> tuple[dict, dict]:
    """
    Open a temporary browser, let the user log into Zoho CRM, then collect
    session cookies and auto-detect the org config from the final URL.

    Returns (cookies_dict, config_dict).
    The temporary browser profile is deleted after use.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit(
            "Playwright is required for browser login. Install it with:\n"
            "  pip install playwright && playwright install chromium\n\n"
            "Alternatively, pass cookies manually:\n"
            "  python zoho_pricebook_export.py --cookies '<paste from browser DevTools>'"
        )

    import shutil
    import tempfile

    tmp_profile = tempfile.mkdtemp(prefix="zoho_login_")
    login_url = f"{_crm_base(config)}/tab/Home/begin" if config else GENERIC_LOGIN_URL

    try:
        with sync_playwright() as pw:
            print("Opening browser for Zoho login...")
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=tmp_profile,
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
                viewport={"width": 1280, "height": 900},
            )
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(login_url, wait_until="networkidle", timeout=30000)

            print("\n" + "=" * 60)
            print("  Please log into Zoho CRM in the browser window.")
            print("=" * 60 + "\n")

            # Wait up to 5 minutes for user to log in and reach a CRM page
            try:
                page.wait_for_url("**/crm/**org**", timeout=300_000)
                time.sleep(3)
            except Exception:
                pass

            if "/crm/" not in page.url:
                input("Press Enter once you're logged in...")

            # Auto-detect org config from the post-login URL
            if not config:
                config = _extract_config_from_url(page.url)
                if config:
                    print(f"  Detected org: {config['org_id']} on {config['domain']}")
                    save_config(config)
                else:
                    sys.exit(
                        "ERROR: Could not detect org ID from URL.\n"
                        f"  Current URL: {page.url}\n"
                        "  Make sure you completed login and reached the CRM dashboard."
                    )

            # Collect cookies from all relevant Zoho domains
            domain = config["domain"]
            raw_cookies = ctx.cookies([
                f"https://crm.{domain}",
                f"https://www.{domain}",
                f"https://accounts.{domain}",
            ])
            cookies = {c["name"]: c["value"] for c in raw_cookies}
            ctx.close()
            print(f"  Got {len(cookies)} cookies.")

            if not _get_csrf(cookies):
                sys.exit("ERROR: No CSRF token found. Login may not have completed.")

            return cookies, config
    finally:
        shutil.rmtree(tmp_profile, ignore_errors=True)


def _get_authenticated_session(args, config: dict | None) -> tuple[dict, dict]:
    """
    Resolve cookies and config through one of three paths:
      1. --cookies flag (manual paste)
      2. --login flag (force browser login)
      3. Cached session file (or auto browser login if missing/expired)

    Returns (cookies_dict, config_dict).
    """
    if args.cookies:
        cookies = parse_cookie_string(args.cookies)
        save_cookies(cookies)
        if not config:
            sys.exit(
                "No org config found. When using --cookies for the first time,\n"
                "run once with --login to auto-detect your org, or create\n"
                f"{SESSION_FILE} manually:\n"
                '  {"org_id": "YOUR_ORG_ID", "domain": "zoho.com"}'
            )
        return cookies, config

    if args.login:
        cookies, config = _login_with_playwright(config)
        save_cookies(cookies)
        return cookies, config

    # Try cached session
    cookies = load_cookies()
    if cookies and config:
        print(f"Using cached session from {SESSION_FILE}")
        return cookies, config

    # No valid session — open browser
    print("No cached session found. Opening browser to log in...")
    cookies, config = _login_with_playwright(config)
    save_cookies(cookies)
    return cookies, config


# ── Price Book listing ─────────────────────────────────────────

def list_pricebooks(session: requests.Session, csrf: str, config: dict) -> list[dict]:
    """
    Fetch all price books via Zoho CRM's internal JSON API.
    Returns a list of dicts: [{id, name, active}, ...]
    """
    url = f"{_api_base(config)}/Price_Books/bulk"
    params = {"page": "1", "per_page": "200", "approved": "both"}
    try:
        resp = session.post(
            url, params=params,
            headers=_api_headers(config, csrf),
            data=API_BODY,
        )
        resp.raise_for_status()
        return [
            {
                "id": item["id"],
                "name": item.get("Price_Book_Name", "?"),
                "active": item.get("Active", False),
            }
            for item in resp.json().get("data", [])
        ]
    except Exception as e:
        print(f"  Error fetching price books: {e}")
        return []


def find_pricebook_rid(session: requests.Session, module_id: str, config: dict) -> str | None:
    """
    Find the Related List ID (RID) for the Products list within Price Books.
    This ID is required by Zoho's ShowMultiValuesForAdd endpoint.

    Tries multiple detection strategies:
      1. Settings API (GET) — /settings/related_lists?module=Price_Books
      2. Settings API (POST with multipart body)
      3. HTML scraping of the price book detail page
    """
    csrf = _get_csrf(dict(session.cookies))
    settings_url = f"{_api_base(config)}/settings/related_lists"
    settings_params = {"module": "Price_Books"}

    # Strategy 1: GET request to settings API
    try:
        resp = session.get(
            settings_url, params=settings_params,
            headers=_api_headers(config, csrf),
        )
        if resp.ok:
            rid = _find_products_rid(resp.json())
            if rid:
                return rid
    except Exception:
        pass

    # Strategy 2: POST with multipart body (some Zoho versions require this)
    try:
        resp = session.post(
            settings_url, params=settings_params,
            headers=_api_headers(config, csrf),
            data=API_BODY,
        )
        if resp.ok:
            rid = _find_products_rid(resp.json())
            if rid:
                return rid
    except Exception:
        pass

    # Strategy 3: Scrape the price book detail page for rid= in HTML/JS
    try:
        detail_url = f"{_crm_base(config)}/tab/PriceBooks/{module_id}"
        resp = session.get(detail_url, headers={**_req_headers(config), "Accept": "text/html"})
        if resp.ok:
            # Look for rid= in URLs or relatedlistId in JavaScript
            for pattern in [r"rid=(\d+)", r'"relatedlistId"\s*:\s*"(\d+)"']:
                match = re.search(pattern, resp.text)
                if match:
                    return match.group(1)
    except Exception:
        pass

    return None


def _find_products_rid(data: dict) -> str | None:
    """Extract the Products related list ID from a settings API response."""
    for rl in data.get("related_lists", []):
        module_info = rl.get("module", {})
        if module_info.get("api_name") == "Products" or rl.get("api_name") == "Products":
            return str(rl["id"])
    return None


# ── HTML parsers ───────────────────────────────────────────────

class _PriceTableParser(HTMLParser):
    """
    Parse the 'Edit List Prices' popup HTML to extract product rows.

    The table structure is:
      <tr>
        <td>[checkbox]</td>     ← td_count=1, skipped
        <td>Column A</td>       ← td_count=2, captured
        <td>Column B</td>       ← td_count=3, captured
        <td>Column C</td>       ← td_count=4, captured
        <td>€ <input name="listPrice1" value="1234"></td>  ← price captured from input
      </tr>

    Each complete row produces a 4-element list: [col_a, col_b, col_c, price].
    """

    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self.headers: list[str] = []
        self._in_td = False
        self._in_th = False
        self._current_row: list[str] = []
        self._current_text = ""
        self._row_started = False
        self._td_count = 0
        self._th_texts: list[str] = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "tr":
            self._current_row, self._td_count, self._row_started = [], 0, True
        elif tag == "th" and self._row_started:
            self._in_th = True
            self._current_text = ""
        elif tag == "td" and self._row_started:
            self._in_td, self._td_count, self._current_text = True, self._td_count + 1, ""
        elif tag == "input" and self._in_td and d.get("name") == "listPrice1":
            self._current_row.append(d.get("value", ""))

    def handle_endtag(self, tag):
        if tag == "th" and self._in_th:
            self._in_th = False
            self._th_texts.append(self._current_text.strip())
        elif tag == "td" and self._in_td:
            self._in_td = False
            # Columns 2, 3, 4 contain product data (column 1 is the checkbox)
            if self._td_count in (2, 3, 4):
                self._current_row.append(self._current_text.strip())
        elif tag == "tr" and self._row_started:
            self._row_started = False
            # Capture header row (first <tr> with <th> elements)
            if self._th_texts and not self.headers:
                self.headers = [h for h in self._th_texts if h]
                self._th_texts = []
            # A complete data row has exactly 4 fields: 3 text columns + 1 price
            if len(self._current_row) == 4:
                self.rows.append(self._current_row)

    def handle_data(self, data):
        if self._in_td or self._in_th:
            self._current_text += data


class _TitleParser(HTMLParser):
    """Extract the price book name from the popup heading (e.g. 'Edit List Prices : My Book')."""

    def __init__(self):
        super().__init__()
        self.title: str | None = None
        self._in_heading = False
        self._text = ""

    def handle_starttag(self, tag, attrs):
        if tag == "td" and "crm-heading-font-size" in dict(attrs).get("class", ""):
            self._in_heading = True
            self._text = ""

    def handle_endtag(self, tag):
        if tag == "td" and self._in_heading:
            self._in_heading = False
            if ":" in self._text:
                self.title = self._text.split(":", 1)[1].strip()

    def handle_data(self, data):
        if self._in_heading:
            self._text += data


def _parse_price_html(html: str) -> tuple[list[list[str]], list[str]]:
    """Parse price table HTML. Returns (rows, column_headers)."""
    parser = _PriceTableParser()
    parser.feed(html)
    return parser.rows, parser.headers


def _parse_title(html: str) -> str | None:
    """Extract the price book name from the popup HTML."""
    parser = _TitleParser()
    parser.feed(html)
    return parser.title


# ── Zoho CRM requests ─────────────────────────────────────────

def _fetch_first_page(
    session: requests.Session, csrf: str,
    module_id: str, rid: str, config: dict,
) -> str:
    """Fetch page 1 of the Edit List Prices popup via ShowMultiValuesForAdd."""
    url = (
        f"{_crm_base(config)}/ShowMultiValuesForAdd.do"
        f"?moduleid={module_id}&frommodule=PriceBooks&tomodule=Products"
        f"&EditAll=true&parentCurrencyISOCode=undefined&parentER=undefined"
        f"&rid={rid}&pname=undefined"
    )
    resp = session.post(url, data=f"crmcsrfparam={csrf}", headers=_req_headers(config))
    resp.raise_for_status()
    return resp.text


def _fetch_next_page(
    session: requests.Session,
    current_from: int, module_id: str, config: dict,
) -> str:
    """
    Fetch the next page via NavigateByRecords.

    Zoho's pagination sends the *current* page's fromIndex/toIndex,
    and the server returns the *next* page. For example:
      Send fromIndex=1,  toIndex=10  → returns records 11-20
      Send fromIndex=11, toIndex=20  → returns records 21-30
    """
    base = _crm_base(config)
    file_name = (
        f"/crm/org{config['org_id']}/ShowMultiValuesForAdd.do"
        f"?moduleid={module_id}&frommodule=PriceBooks&tomodule=Products"
        f"&returnAnchor=productspersonality&EditAll=true"
    )
    body = (
        f"moduleid={module_id}"
        f"&previousCVID="
        f"&sortOrderString=null"
        f"&category=0"
        f"&tomodule=Products"
        f"&frommodule=PriceBooks"
        f"&idlist="
        f"&pricelist="
        f"&parentCurrencyISOCode=null"
        f"&parentER=null"
        f"&relAttr=List"
        f"&cvid=null"
        f"&sortColumnforshowmultivalue=null"
        f"&returnAnchor=productspersonality"
        f"&display_cust_view_name=null"
        f"&searchString="
        f"&fromIndex={current_from}"
        f"&rangeValue={PAGE_SIZE}"
        f"&toIndex={current_from + PAGE_SIZE - 1}"
        f"&OnSelect=true"
        f"&currentOption={PAGE_SIZE}"
        f"&totalRecords=-1"
        f"&fileName={urllib.parse.quote(file_name, safe='')}"
        f"&"
    )
    resp = session.post(
        f"{base}/NavigateByRecords.do?next.x=Xaa&next.y=Ybb",
        data=body, headers=_req_headers(config),
    )
    resp.raise_for_status()
    return resp.text


# ── Export logic ───────────────────────────────────────────────

def export_pricebook(
    cookies: dict, module_id: str, rid: str,
    output: str, config: dict, pb_name: str | None = None,
) -> bool:
    """
    Export all products and list prices for a price book to CSV.
    Paginates through all pages automatically.
    Returns True on success, False if no data found.
    """
    csrf = _get_csrf(cookies)
    session = requests.Session()
    session.cookies.update(cookies)

    all_rows: list[list[str]] = []
    col_headers: list[str] | None = None

    # Fetch page 1
    print("Fetching page 1...")
    html = _fetch_first_page(session, csrf, module_id, rid, config)

    # Extract price book name from the popup heading
    title = _parse_title(html)
    if title:
        print(f"  Price Book: {title}")
        if not pb_name:
            pb_name = title

    rows, headers = _parse_price_html(html)
    if headers:
        col_headers = headers
    print(f"  Got {len(rows)} records.")
    all_rows.extend(rows)

    if not rows:
        return False

    # Fetch remaining pages via NavigateByRecords
    current_from = 1  # send current page's fromIndex; server returns next page
    page_num = 2
    while True:
        print(f"Fetching page {page_num}...")
        time.sleep(0.3)  # polite delay
        try:
            html = _fetch_next_page(session, current_from, module_id, config)
        except requests.HTTPError:
            break

        rows, _ = _parse_price_html(html)
        if not rows:
            print("  No more records.")
            break

        print(f"  Got {len(rows)} records.")
        all_rows.extend(rows)
        current_from += PAGE_SIZE

        if len(rows) < PAGE_SIZE:
            break
        page_num += 1

    # Deduplicate by first column (product name)
    seen: set[str] = set()
    unique: list[list[str]] = []
    for row in all_rows:
        if row[0] not in seen:
            seen.add(row[0])
            unique.append(row)

    # Auto-generate filename from price book name if user didn't specify --output
    if pb_name and output == OUTPUT_FILE:
        safe_name = re.sub(r"[^\w\s-]", "", pb_name).strip().replace(" ", "_")
        output = f"{safe_name}.csv"

    # Build CSV headers: use table headers from Zoho if available, else generic
    if col_headers and len(col_headers) >= 3:
        csv_headers = [h for h in col_headers if h]
    else:
        csv_headers = ["Column 1", "Column 2", "Column 3", "List Price"]

    # Ensure header count matches row width
    while len(csv_headers) < 4:
        csv_headers.append(f"Column {len(csv_headers) + 1}")
    csv_headers = csv_headers[:4]

    with open(output, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(csv_headers)
        writer.writerows(unique)

    print(f"\nExported {len(unique)} records to {output}")
    return True


# ── Interactive picker ─────────────────────────────────────────

def _pick_pricebook_interactive(pbs: list[dict]) -> tuple[str, str]:
    """Display a numbered list of price books and let the user choose one."""
    print("\nAvailable price books:\n")
    for i, pb in enumerate(pbs, 1):
        status = "active" if pb.get("active") else "inactive"
        print(f"  {i}) {pb['name']}  [{status}]")

    print()
    choice = input(f"Choose (1-{len(pbs)}), or 'q' to quit: ").strip()
    if choice.lower() in ("q", "quit", ""):
        sys.exit(0)
    try:
        idx = int(choice) - 1
        if not (0 <= idx < len(pbs)):
            raise ValueError
    except ValueError:
        sys.exit(f"Invalid choice: {choice}")

    return pbs[idx]["id"], pbs[idx]["name"]


def _find_pricebook_by_name(
    pbs: list[dict], search: str,
) -> tuple[str, str]:
    """Find a price book by partial name match. Exits on no match or ambiguity."""
    matches = [pb for pb in pbs if search.lower() in pb["name"].lower()]

    if not matches:
        print(f"No price book found matching '{search}'.")
        if pbs:
            print("Available:")
            for pb in pbs:
                print(f"  {pb['id']}  {pb['name']}")
        sys.exit(1)

    if len(matches) > 1:
        print("Multiple matches:")
        for pb in matches:
            print(f"  {pb['id']}  {pb['name']}")
        print("Be more specific or use the full ID.")
        sys.exit(1)

    return matches[0]["id"], matches[0]["name"]


# ── Main ───────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(
        description="Export Zoho CRM Price Book products and list prices to CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "First run opens a browser — log in and your org is auto-detected.\n\n"
            "Examples:\n"
            "  %(prog)s                            Interactive picker\n"
            "  %(prog)s --list                     Show all price books\n"
            '  %(prog)s --pricebook "My Book"      Export by name (partial match)\n'
            "  %(prog)s --pricebook 1234567890     Export by ID\n"
            "  %(prog)s --login                    Force re-login\n"
        ),
    )
    ap.add_argument("--pricebook", help="Price book ID or name (partial match)")
    ap.add_argument("--rid", help="Related list ID (auto-detected if omitted)")
    ap.add_argument("--list", action="store_true", help="List all available price books")
    ap.add_argument("--cookies", help="Manual cookie string from browser DevTools")
    ap.add_argument("--login", action="store_true", help="Force re-login via browser")
    ap.add_argument("--output", default=OUTPUT_FILE, help="Output CSV filename")
    args = ap.parse_args()

    # Authenticate
    config = load_config()
    cookies, config = _get_authenticated_session(args, config)
    csrf = _get_csrf(cookies)

    session = requests.Session()
    session.cookies.update(cookies)

    # --list: print price books and exit
    if args.list:
        print("Fetching price books...")
        pbs = list_pricebooks(session, csrf, config)
        if pbs:
            print(f"\nFound {len(pbs)} price book(s):\n")
            for pb in pbs:
                status = "active" if pb.get("active") else "inactive"
                print(f"  {pb['id']}  {pb['name']}  [{status}]")
        else:
            print("Could not fetch price book list.")
        return

    # Determine which price book to export
    if not args.pricebook:
        # Interactive mode
        print("Fetching price books...")
        pbs = list_pricebooks(session, csrf, config)
        if not pbs:
            sys.exit("Could not fetch price book list. Try --login to refresh session.")
        module_id, pb_name = _pick_pricebook_interactive(pbs)
        print(f"\nSelected: {pb_name}")
    elif args.pricebook.isdigit() and len(args.pricebook) > 10:
        # Direct ID
        module_id = args.pricebook
        pb_name = None  # will be extracted from the page
    else:
        # Search by name
        print(f"Searching for price book matching '{args.pricebook}'...")
        pbs = list_pricebooks(session, csrf, config)
        module_id, pb_name = _find_pricebook_by_name(pbs, args.pricebook)
        print(f"  Found: {pb_name} ({module_id})")

    # Determine RID (cached after first detection)
    rid = args.rid or config.get("rid")
    if not rid:
        print("Detecting related list ID...")
        rid = find_pricebook_rid(session, module_id, config)
        if rid:
            print(f"  RID: {rid}")
            config["rid"] = rid
            save_config(config)
        else:
            sys.exit(
                "Could not auto-detect RID. Pass it manually with --rid.\n"
                "Find it in the Edit List Prices URL: ...rid=<THIS_VALUE>"
            )

    # Export
    try:
        success = export_pricebook(cookies, module_id, rid, args.output, config, pb_name)
    except requests.HTTPError:
        success = False

    if not success:
        print("\nCookies may have expired. Opening browser to log in...")
        cookies, config = _login_with_playwright(config)
        save_cookies(cookies)
        export_pricebook(cookies, module_id, rid, args.output, config, pb_name)


if __name__ == "__main__":
    main()
