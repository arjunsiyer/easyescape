# Easy Escape

Checks escape room availability across Reno / Sparks, NV venues and prints available time slots.

## Venues covered

- Puzzle Room Reno (Checkfront)
- Break Through Reno (Checkfront)
- Sensology (Checkfront)
- Key & Code — Sparks, South Reno, Costco Center (FareHarbor)
- Deadline Escape Rooms (FareHarbor)
- Keystone Escape Games (Bookeo)
- Brainy Actz — phone only, no online booking for rooms

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## Run

```bash
python3 check_availability.py
```

Opens a browser window (required to bypass bot detection on some booking sites) and prints availability for today.
