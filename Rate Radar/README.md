# BMAP Rate Radar

Scrapes advertised CD, savings, checking, and money market rates from bank websites.
Results save automatically to CSV and to Supabase after each run.

---

## Setup (one time)

```bash
pip install -r requirements.txt
playwright install chromium
```

---

## Run

```bash
python rate_radar.py
```

Opens a browser at http://localhost:7331

1. Upload a CSV with columns: `bank_name`, `bank_url`
2. Click Start Crawl
3. Results save automatically to `RateRadar_Exports/` and to Supabase

---

## Save to Supabase

Create a `.env` file in the same folder as `rate_radar.py`:

```
SUPABASE_SERVICE_KEY=your_service_role_key_here
```

That's it. The script reads it automatically every time you run it.
No environment variables to set. No key to remember.

Your service role key is in Supabase → Settings → API → service_role.
The `.env` file is listed in `.gitignore` — it will never be uploaded to GitHub.

---

## CSV format

```csv
bank_name,bank_url
Hancock Whitney Bank,https://www.hancockwhitney.com
Regions Bank,https://www.regions.com
```

---

## Output

- `RateRadar_Exports/rate_radar_YYYYMMDD_HHMMSS.csv` — local archive
- Supabase `raw_rate_radar` table — feeds the live viewer on the website
