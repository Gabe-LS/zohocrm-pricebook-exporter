# zohocrm-pricebook-export

Export all products and list prices from a Zoho CRM Price Book to CSV.

> **Note:** tested on macOS only. Should work on Linux and Windows but has not been verified.

---

## Before you start — check Python is installed

You need **Python 3.10 or later** on your computer.

**macOS:** open the Terminal app (press `Command + Space`, type `Terminal`, press Enter), then type:

```
python3 --version
```

**Windows:** press `Windows + R`, type `cmd`, press Enter, then type:

```
python --version
```

If you see `Python 3.10` or a higher number, you're good. If you see `Python 2.x`, an error, or nothing — download and install Python 3 from [python.org](https://www.python.org/downloads/).

> **Windows tip:** during the Python installation, make sure to check the box that says **"Add Python to PATH"** — without this, the commands in this guide will not work.

---

## Step 1 — Download the tool

1. Click the green **Code** button at the top of this page
2. Click **Download ZIP**
3. Unzip the downloaded file — you'll get a folder called `zohocrm-pricebook-export-main`
4. Remember where you saved that folder — you'll need it in the next step

---

## Step 2 — Open a terminal in the tool folder

**macOS:**
1. Open the **Terminal** app — press `Command + Space`, type `Terminal`, press Enter
2. Open **Finder** and navigate to the `zohocrm-pricebook-export-main` folder — it should be in your `Downloads` folder
3. In Terminal, type `cd ` (the letters c, d, and a space — do not press Enter yet)
4. Click on the `zohocrm-pricebook-export-main` folder in Finder, then drag it into the Terminal window — the folder path will appear automatically after `cd `
5. Press Enter

**Windows:**
1. Open **File Explorer** — press `Windows + E` or click the folder icon in the taskbar
2. Navigate to the `zohocrm-pricebook-export-main` folder — it should be in your `Downloads` folder
3. Click once on the address bar at the top of the File Explorer window (the bar that shows the folder path — it will turn blue and show the full path)
4. Type `cmd` (replacing whatever was there) and press Enter — a Command Prompt window will open in the right folder

---

## Step 3 — Install dependencies

In the terminal you opened in Step 2, type the following commands and press Enter after each one:

**macOS / Linux:**
```
pip3 install requests playwright
playwright install chromium
```

**Windows:**
```
pip install requests playwright
playwright install chromium
```

This may take a few minutes depending on your internet speed. **You only need to do this once.**

> **Note:** Playwright is used to open a browser window for Zoho login. If you prefer not to install it, see the "Manual cookies" section under Technical details.

---

## Step 4 — Run

In the terminal (open it again following Step 2 if you closed it), type:

**macOS / Linux:**
```
python3 zoho_pricebook_export.py
```

**Windows:**
```
python zoho_pricebook_export.py
```

### First run — log in

A browser window will open showing the Zoho login page. Log into your Zoho CRM account as you normally would. Once you're in, the tool will automatically detect your organization and save the session.

You only need to log in once — the session is cached for future runs. If it expires, the browser will open again automatically.

### Choose a price book

After login, you'll see a list of your price books:

```
Available price books:

  1) Retail Partners 2024-2026  [active]
  2) Wholesale Distributors 2024  [active]
  3) Legacy Price List 2020  [inactive]

Choose (1-3), or 'q' to quit: 1
```

Type the number of the price book you want and press Enter. The tool will download all products and list prices and save them as a CSV file in the same folder:

```
Selected: Retail Partners 2024-2026
Fetching page 1...
  Got 10 records.
Fetching page 2...
  Got 10 records.
Fetching page 3...
  Got 4 records.

Exported 24 records to Retail_Partners_2024-2026.csv
```

### Other ways to run

```
# List all price books without exporting
python3 zoho_pricebook_export.py --list

# Export by name (partial match — no need to type the full name)
python3 zoho_pricebook_export.py --pricebook "Retail"

# Export by ID (from the URL: crm.zoho.com/.../tab/PriceBooks/<ID>)
python3 zoho_pricebook_export.py --pricebook 1234567890123456789

# Save to a specific filename
python3 zoho_pricebook_export.py --pricebook "Retail" --output prices.csv

# Force re-login (if the session expired)
python3 zoho_pricebook_export.py --login
```

> **Windows:** replace `python3` with `python` in all commands above.

---

## Something not working?

Open an issue at [github.com/Gabe-LS/zohocrm-pricebook-export/issues](https://github.com/Gabe-LS/zohocrm-pricebook-export/issues)

---

## Features

- **Zero config** — your Zoho org and domain are auto-detected from the login
- **Interactive picker** — run with no arguments to choose a price book from a list
- **Search by name** — partial match with `--pricebook "keyword"`
- **All Zoho domains** — works with zoho.com, zoho.eu, zoho.in, zoho.com.au
- **Smart filenames** — output CSV is named after the price book
- **Session caching** — log in once, run as many times as you need
- **Auto-recovery** — if the session expires mid-export, the browser reopens automatically

---

## Technical details

<details>
<summary>Click to expand</summary>

**How it works**

Zoho CRM has no built-in way to export price book list prices. The UI shows them in a paginated popup (10 at a time) with no download option.

This tool authenticates via a temporary Chromium browser (Playwright), then uses Zoho's internal web endpoints to fetch the data:

1. **Login** — opens a temporary browser, captures session cookies after login, then closes and deletes the browser profile
2. **Session caching** — cookies and org config are stored in `zohocrm_session.json` (file permissions `600`) next to the script
3. **Price book listing** — uses `POST /crm/v2.2/Price_Books/bulk` (internal JSON API)
4. **Price export** — fetches the "Edit List Prices" popup via `ShowMultiValuesForAdd.do` (page 1) and `NavigateByRecords.do` (pages 2+), parsing the HTML table
5. **Related list detection** — automatically finds the Products related list ID via the settings API

This relies on undocumented Zoho internals that may change at any time.

**Limitations**

- Tested on macOS only
- Zoho forces 10 records per page in the Edit List Prices popup — the script paginates automatically but makes one request per page
- Requires an interactive login for the first run — there is no headless/unattended mode for authentication
- The session may expire after some time — re-run the script and it will re-authenticate automatically

**Manual cookies**

If you don't want to install Playwright, you can pass cookies manually. In your browser, open Zoho CRM, then open DevTools (press `F12`), go to the Network tab, right-click any request to `crm.zoho.com`, click "Copy as cURL", and extract the cookie string (the value after `-b`). Then run:

**macOS / Linux:**
```
python3 zoho_pricebook_export.py --cookies 'JSESSIONID=abc; crmcsr=xyz; ...'
```

**Windows:**
```
python zoho_pricebook_export.py --cookies "JSESSIONID=abc; crmcsr=xyz; ..."
```

Note: the first time you use `--cookies`, you also need a `zohocrm_session.json` file with your org config. Run once with `--login` to create it automatically, or create it manually:

```json
{"org_id": "YOUR_ORG_ID", "domain": "zoho.com"}
```

Find your org ID in any Zoho CRM URL: `https://crm.zoho.com/crm/org<THIS_NUMBER>/tab/...`

**Files**

| File | Purpose |
|------|---------|
| `zoho_pricebook_export.py` | The script |
| `zohocrm_session.json` | Cached session — auto-created, gitignored |
| `*.csv` | Exported price lists |

</details>

---

## License

MIT License — Copyright (c) 2025 Gabriele Lo Surdo

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
