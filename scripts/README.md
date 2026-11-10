# Scripts

## find_events.py

Finds candidate events with the Serper API, opens each organiser page once (robots.txt aware), reads dates, speaker and sponsor links, price and audience terms, then scores the event on the 50-point rubric and picks the top ones per market.

```bash
pip install -r requirements.txt
cp .env.example .env          # then put your Serper key in .env
python find_events.py --selftest          # offline check, no key needed
python find_events.py --markets US UK DE
python find_events.py --no-verify         # search only, no page fetching
```

Nothing is invented: a value the script cannot read stays `?` or empty. Pages it cannot open are written to `manual_needed.csv`. Review the top 15 by hand before you decide.

Set `COMPANY_NAME` in `.env` to also search LinkedIn for event posts that mention your company. Leave it empty to skip that source.

The buyer profile (keywords, associations, vendors to drop) sits in the config block near the top. That block is what you change for a new company.

## check_secrets.sh

Scans the repo for keys, tokens, webhook URLs and email addresses. Run it before every push.

## Rules for this folder

- Keys come from environment variables or `.env`, never from the file.
- `out/` and `manual/` can hold names and page text, so they are git-ignored.
