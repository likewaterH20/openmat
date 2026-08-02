# OPENMAT — NJ Open Mat Finder

## What this is
"Weedmaps for jiu-jitsu." V1 is a New Jersey BJJ open mat finder, live at
https://likewaterh20.github.io/openmat/ via GitHub Pages (repo: likewaterH20/openmat).
Built for the owner's friend who trains BJJ. Owner (Rafael) is non-technical:
explain things in plain language, no jargon, handle execution end to end.

## Architecture (keep it this way)
- ONE static file: index.html. No backend, no build step, no API keys in the app.
- The database is the `const MATS = [...]` array inside index.html.
- Push to main = auto-deploy via GitHub Pages within ~1 minute.
- Stack inside the file: vanilla JS, Leaflet (cdnjs) + CARTO tiles, Nominatim
  for zip/city geocoding, Google Fonts (Anton, Inter, IBM Plex Mono).

## Data model (every MATS entry)
gym, town, county, lat, lng, day (0=Sun..6=Sat, null = "on gym schedule"),
start/end (strings, "10:00 AM"), startH/endH (24h decimals, must match strings),
format ("Gi" | "No-Gi" | "Gi + No-Gi" | "Check gym"), cost, verified
("confirmed" = seen on gym's own site | "reported" = directory-sourced),
verifiedDate, site, ig.

## Data rules
- NEVER invent times, prices, or IG handles. Unknown = "Check gym" / "Ask gym".
- Gym's own website always overrides directories (openmatlocator.com etc.).
- Cancellations are the top priority: a wrong listing is worse than a missing one.
- Update cadence: daily light sweep for cancellations/closures (check comp
  calendars too — JJWL at Cure Arena Trenton, IBJJF, Grappling Industries empty
  mats on tournament weekends); full re-verify + expansion every 7 days.
- Always bump LAST_UPDATE and per-entry verifiedDate when checked.

## Product decisions already made
- Name: OPENMAT (one word). "Roll Call" reserved for the daily schedule feature.
- Web app first, native later. Not BJJ-only forever (open classes, e.g. tai chi, later).
- Post a Mat form emails submissions for manual verification. SUBMIT_EMAIL in the
  CONFIG block is still a placeholder — ask Rafael for the address.
- Email accounts (Supabase magic links) = V2, after real usage. Profile/belt/waiver
  vault = the long-term moat. Business model: gyms pay to claim listings later.
- Design: white-studio minimal, Anton display type, tatami-blue accent (#1E3FE0),
  Day/Night switch. Keep UI text minimal.

## Backlog (rough priority)
1. Wire real SUBMIT_EMAIL
2. Grow listings past 12 (South Jersey is thin; Totowa, Fair Lawn, Toms River
   have mats per openmatlocator not yet added)
3. Confirm amber "reported" entries on gym sites -> flip to green
4. "CHECK BEFORE GOING" flag state for suspected closures
5. V2: accounts, gym claim flow, reviews

## Daily sweep (automated)
`.github/workflows/daily-sweep.yml` runs `.github/scripts/sweep.py` every morning
at 7:00 AM ET. It fetches each gym's own site, pulls the schedule-ish lines, and
diffs them against `.github/data/snapshots.json`.
- It NEVER edits gym data. Changes open an issue; a person verifies and edits MATS.
- Quiet day = it bumps LAST_UPDATE and commits.
- Mondays it lists the gyms whose schedule is an image or a JS widget — those are
  unreadable to the robot and only get checked when a person looks.
- Run it on demand from the Actions tab, or locally: `python3 .github/scripts/sweep.py --dry-run`
