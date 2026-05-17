#!/usr/bin/env python3
"""Check escape room availability and time slots across Reno/Sparks venues."""

import argparse
import asyncio
import datetime
import math
import os
import re
from datetime import date
from playwright.async_api import async_playwright, Page
from rich.console import Console
from rich.table import Table
from rich import box
from rich.text import Text

console = Console(highlight=False)

# Globals (preserved for CLI backwards compatibility, but avoided in module functions)
_DEFAULT_TARGET_DATE = date.today()

def get_date_strings(d: date):
    return {
        "str": d.strftime("%Y%m%d"),      # 20260418
        "iso": d.strftime("%Y-%m-%d"),    # 2026-04-18
        "label": d.strftime("%B %-d, %Y")
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def is_time(line: str) -> bool:
    return bool(re.match(r"\d{1,2}:\d{2}\s*(AM|PM)", line.strip()))


async def goto(page: Page, url: str, timeout: int = 20000):
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        await page.wait_for_timeout(3000)
    except Exception:
        pass


_CF_SKIP = {"Book Now", "Details", "Availability", "AVAILABLE", "SOLD OUT", "UNKNOWN",
            "Photos", "Close Continue", "Available", "Sold out", "Unavailable",
            "New Booking", "Category"}


def parse_checkfront_listing(text: str) -> list[tuple[str, str]]:
    """Return ordered [(room_name, status)] from a Checkfront listing page."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    results: list[tuple[str, str]] = []
    
    # Checkfront listing: "Availability" usually PRECEDES the Room Name in the text dump
    for i, line in enumerate(lines):
        if line.strip() == "Availability":
            status = "AVAILABLE"
            # Check a few lines before for status
            for k in range(max(0, i-5), i):
                if lines[k] in ("AVAILABLE", "SOLD OUT"):
                    status = lines[k]
                    break
            
            # Look forward for the room name
            room_name = "Unknown Room"
            for j in range(i + 1, min(i + 12, len(lines))):
                c = lines[j]
                if (c not in _CF_SKIP and not c.startswith("$") and len(c) > 2 
                    and not re.match(r"^\d+(\.\d+)?$", c)
                    and "Sat " not in c and "Sun " not in c and "Mon " not in c
                    and "May " not in c and "June " not in c and "July " not in c):
                    room_name = c
                    break
            results.append((room_name, status))
    return results


def parse_fareharbor_timeslots(text: str) -> dict[str, list[str]]:
    """Group FareHarbor time slots by room name. Slots look like '7:15 PM Room Name | Location'."""
    rooms: dict[str, list[str]] = {}
    pattern = re.compile(r"(\d{1,2}:\d{2}\s*(?:AM|PM))\s+(.+?)(?:\s*\|\s*.+)?$")
    for line in text.splitlines():
        m = pattern.match(line.strip())
        if m:
            time_part = m.group(1).strip()
            name_part = m.group(2).strip()
            # Strip location suffix (everything after " | ")
            name_part = re.sub(r"\s*\|.*$", "", name_part).strip()
            rooms.setdefault(name_part, []).append(time_part)
    return rooms


# ── Checkfront scraper ────────────────────────────────────────────────────────

async def scrape_checkfront(page: Page, target_date: date, subdomain: str, category_id: str = None) -> list[dict]:
    ds = get_date_strings(target_date)
    params = f"?D={ds['str']}"
    if category_id:
        params += f"&category_id={category_id}"
    url = f"https://{subdomain}.checkfront.com/reserve/{params}"

    await goto(page, url)
    try:
        # Wait for either an item or the 'no items' message
        await page.wait_for_selector(".cf-item, a:has-text('Availability'), .cf-no-items", timeout=12000)
    except Exception:
        pass

    await page.wait_for_timeout(3000)
    page_text = await page.evaluate("() => document.body.innerText")
    
    if "Nothing available" in page_text or "no items" in page_text.lower():
        return [{"room": "—", "status": "NO AVAILABILITY TODAY", "times": []}]

    listing = parse_checkfront_listing(page_text)
    
    # Re-fetch buttons
    btns = await page.query_selector_all("a:has-text('Availability')")
    
    if not btns:
        if listing:
             return [{"room": r, "status": s, "times": []} for r, s in listing]
        return [{"room": "—", "status": "NO DATA", "times": []}]

    results = []
    # If we have multiple rooms, we need to click each one to get times
    # To keep it fast and reliable, we'll try to get times for each
    for i in range(len(btns)):
        room, status = listing[i] if i < len(listing) else (f"Room {i + 1}", "UNKNOWN")
        
        if status == "SOLD OUT":
            results.append({"room": room, "status": "SOLD OUT", "times": []})
            continue

        # For the first one, we are already on the page. For others, we might need to go back or re-nav.
        if i > 0:
            await goto(page, url)
            await page.wait_for_timeout(2000)
            current_btns = await page.query_selector_all("a:has-text('Availability')")
            if i < len(current_btns):
                btn = current_btns[i]
            else:
                results.append({"room": room, "status": status, "times": []})
                continue
        else:
            btn = btns[i]

        try:
            await btn.click(force=True)
            await page.wait_for_timeout(3000)

            # Check for day link or direct times
            day_links = await page.query_selector_all(f"a[href*='#D{ds['str']}']")
            if day_links:
                await day_links[-1].click(force=True)
                await page.wait_for_timeout(2000)

            modal_text = await page.evaluate("() => document.body.innerText")
            if "verify you're not a robot" in modal_text.lower():
                times = ["BOT CHECK"]
            else:
                times = [l.strip() for l in modal_text.splitlines() if is_time(l)]
            results.append({"room": room, "status": status, "times": times})
        except Exception as e:
            results.append({"room": room, "status": status, "times": []})

    return results


# ── FareHarbor scraper ────────────────────────────────────────────────────────

_SKIP_LINES = {
    "Select date", "View photos", "Photos", "Details", "Book Now",
    "Availability", "Close", "per person", "per booking", "per group",
    "1 hour", "60 min", "1 Hour",
}


def _looks_like_tagline(text: str) -> bool:
    """Return True if text looks like a marketing description rather than a room title."""
    t = text.lower()
    return (
        len(text) > 55
        or bool(re.search(r"\bminutes?\b", t))
        or text.endswith("!")
        or text.endswith("?")
        or "from $" in t
        or t.startswith("$")
    )


def _extract_fareharbor_room_names(listing_text: str) -> list[str]:
    """Parse listing page text to find room titles before each 'Select date'.

    Skips tagline/description lines (long sentences, '60 minutes...') and picks
    the first short, title-like line when searching backward from 'Select date'.
    """
    lines = [l.strip() for l in listing_text.splitlines() if l.strip()]
    names = []
    # Find indices of all "Select date" / "Buy" buttons
    trigger_indices = [i for i, line in enumerate(lines) if line in ("Select date", "Buy")]
    
    for i in trigger_indices:
        found = None
        for j in range(i - 1, max(i - 20, -1), -1):
            c = lines[j]
            if (not c
                    or c in _SKIP_LINES
                    or c.startswith("$")
                    or "from $" in c.lower()
                    or re.match(r"^\d+(\.\d+)?$", c)
                    or "per " in c.lower()):
                continue
            if not _looks_like_tagline(c):
                found = c
                break
        names.append(found or f"Room {len(names) + 1}")
    return names


async def scrape_fareharbor(page: Page, target_date: date, shortname: str, flow: str = None) -> list[dict]:
    url = f"https://fareharbor.com/embeds/book/{shortname}/?full-items=yes"
    if flow:
        url += f"&flow={flow}"
    await goto(page, url, timeout=25000)

    select_btns = await page.query_selector_all("button:has-text('Select date'), a:has-text('Select date'), button:has-text('Buy'), a:has-text('Buy')")
    if not select_btns:
        return [{"room": "—", "status": "ERROR", "times": []}]

    # Extract all room names from listing text before clicking anything
    listing_text = await page.evaluate("() => document.body.innerText")
    room_names = _extract_fareharbor_room_names(listing_text)

    results = []
    for i in range(len(select_btns)):
        room_name = room_names[i] if i < len(room_names) else f"Room {i + 1}"
        if "gift card" in room_name.lower():
            continue

        await goto(page, url, timeout=25000)
        btns = await page.query_selector_all("button:has-text('Select date'), a:has-text('Select date'), button:has-text('Buy'), a:has-text('Buy')")
        if i >= len(btns):
            break

        await btns[i].click(force=True)
        await page.wait_for_timeout(2000)

        today_cls = await page.evaluate(f"""() => {{
            for (const td of document.querySelectorAll('td')) {{
                const btn = td.querySelector('button');
                if (btn && btn.innerText.trim() === '{target_date.day}') return btn.className || 'found';
            }}
            return 'not_found';
        }}""")

        if "empty" in today_cls or "past-day" in today_cls:
            results.append({"room": room_name, "status": "NO AVAILABILITY TODAY", "times": []})
            continue

        clicked = await page.evaluate(f"""() => {{
            for (const td of document.querySelectorAll('td')) {{
                const btn = td.querySelector('button');
                if (btn && btn.innerText.trim() === '{target_date.day}' && !btn.disabled && !(btn.className || "").includes('empty') && !(btn.className || "").includes('past-day')) {{
                    btn.click();
                    return true;
                }}
            }}
            return false;
        }}""")

        if not clicked:
            results.append({"room": room_name, "status": "NO AVAILABILITY TODAY", "times": []})
            continue

        await page.wait_for_timeout(2500)
        text = await page.evaluate("() => document.body.innerText")
        # FareHarbor slots can be "7:15 PM Room Name | Location" or just "7:15 PM"
        # We want to match the time at the beginning of the line.
        time_pattern = re.compile(r"^(\d{1,2}:\d{2}\s*(?:AM|PM))", re.IGNORECASE)
        times = []
        for line in text.splitlines():
            line = line.strip()
            m = time_pattern.match(line)
            if m:
                times.append(m.group(1).strip())

        status = "AVAILABLE" if times else "NO AVAILABILITY TODAY"
        results.append({"room": room_name, "status": status, "times": times})

    return results or [{"room": "—", "status": "NO DATA", "times": []}]


# ── Print helpers ─────────────────────────────────────────────────────────────

STATUS_STYLE = {
    "AVAILABLE": "bold green",
    "SOLD OUT": "bold red",
    "NO AVAILABILITY TODAY": "yellow",
}


def print_all_venues(
    target_date: date,
    venues: list[tuple[str, str]],
    all_rooms: list[list[dict]],
    manual: list[tuple[str, str, str | None]] | None = None,
):
    """Print a single unified table: Venue | Room | Status | Time Slots."""
    ds = get_date_strings(target_date)
    table = Table(
        title=f"[bold]Reno / Sparks Escape Room Availability — {ds['label']}[/bold]",
        title_justify="left",
        box=box.SIMPLE_HEAVY,
        show_lines=False,
        expand=False,
        min_width=100,
    )
    table.add_column("Venue", style="bold cyan", no_wrap=True, min_width=32)
    table.add_column("Room", style="white", no_wrap=True, min_width=26)
    table.add_column("Status", no_wrap=True, min_width=22)
    table.add_column("Time Slots", min_width=30)

    for idx, ((name, _url), rooms) in enumerate(zip(venues, all_rooms)):
        if idx > 0:
            table.add_section()
        for i, r in enumerate(rooms):
            venue_cell = Text(name if i == 0 else "", style="bold cyan")
            status_style = STATUS_STYLE.get(r["status"], "white")
            status_cell = Text(r["status"], style=status_style)
            
            # Truncate room name if too long for Keystone or others
            room_name = r["room"]
            if len(room_name) > 60:
                room_name = room_name[:57] + "..."
                
            if r["times"]:
                time_cell = Text("  ".join(r["times"]))
            else:
                time_cell = Text("—", style="dim")
            table.add_row(venue_cell, room_name, status_cell, time_cell)

    if manual:
        for venue_name, note, phone in manual:
            table.add_section()
            note_text = note + (f"  {phone}" if phone else "")
            time_cell = Text(note_text, style="dim italic")
            table.add_row(Text(venue_name, style="bold cyan"), Text("—", style="dim"), Text("—", style="dim"), time_cell)

    console.print()
    console.print(table)


# ── Bookeo scraper ────────────────────────────────────────────────────────────

async def scrape_bookeo(page: Page, target_date: date, url: str) -> list[dict]:
    """Returns list of {room, status, times} for a Bookeo venue."""
    await goto(page, url, timeout=30000)
    # Wait for room listing to render (JS-heavy under parallel load)
    try:
        await page.wait_for_selector(".bookeo_items_list", timeout=15000)
    except Exception:
        pass

    text = await page.evaluate("() => document.body.innerText")
    if "verify you're not a robot" in text:
        return [{"room": "—", "status": "BOT CHECK — run non-headless", "times": []}]

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    rooms: dict[str, dict] = {}
    current_room = None
    
    # Bookeo uses "Sat, May 16, 2026" style
    target_date_label = target_date.strftime("%a, %B %-d, %Y")
    target_date_label_alt = target_date.strftime("%a, %B %d, %Y")

    i = 0
    while i < len(lines):
        line = lines[i]
        # Bookeo rooms often have "1 hour" or "60 min" duration listed right after the name
        if i + 1 < len(lines) and lines[i + 1] in ("1 hour", "60 min", "1 Hour", "75 min", "90 min"):
            room_label = line
            # If the line is too long, it might be a description, look one line back
            if len(line) > 55 and i > 0 and len(lines[i-1]) < 55:
                room_label = lines[i-1]
            elif len(line) > 55:
                # If both current and previous are long, try to truncate or find another candidate
                room_label = line.split('.')[0].split('!')[0].split('?')[0][:50].strip()
            
            current_room = room_label
            rooms.setdefault(current_room, {"status": "AVAILABLE", "times": []})
        
        elif (line == target_date_label or line == target_date_label_alt) and current_room:
            j = i + 1
            while j < len(lines):
                next_line = lines[j]
                if is_time(next_line):
                    # Check if next line is "FULL" or similar
                    is_full = False
                    if j + 1 < len(lines) and lines[j+1] in ("FULL", "SOLD OUT", "Sold out"):
                        is_full = True
                    
                    if not is_full:
                        rooms[current_room]["times"].append(next_line)
                elif next_line.startswith("Sun, ") or next_line.startswith("Mon, ") or next_line.startswith("Tue, "):
                    # Hit next day
                    break
                elif j > i + 30: # Safety break
                    break
                j += 1
            i = j
            continue
        i += 1

    if not rooms:
        return [{"room": "—", "status": "NO DATA", "times": []}]

    return [
        {"room": name, "status": "AVAILABLE" if d["times"] else "NO AVAILABILITY TODAY", "times": d["times"]}
        for name, d in rooms.items()
    ]


# ── Main ──────────────────────────────────────────────────────────────────────

KEYSTONE_URL = "https://bookeo.com/keystoneescapegames"

MAX_PARALLEL = max(1, math.floor((os.cpu_count() or 4) * 0.6))

VENUES = [
    ("Puzzle Room Reno",                         "https://puzzleroom.checkfront.com/reserve/"),
    ("Puzzle Room Reno (Cat 2)",                 "https://puzzleroom.checkfront.com/reserve/?category_id=2"),
    ("Break Through Reno",                        "https://gsr.breakthroughreno.com/book-now"),
    ("Sensology",                                 "https://sensology.checkfront.com/reserve/"),
    ("Key & Code — Sparks / Outlets at Legends",  "https://fareharbor.com/embeds/book/keyandcode/?full-items=yes&flow=1587088"),
    ("Key & Code — South Reno / Summit Mall",     "https://fareharbor.com/embeds/book/keyandcode/?full-items=yes&flow=1587108"),
    ("Key & Code — Reno / Costco Center",         "https://fareharbor.com/embeds/book/keyandcode/?full-items=yes&flow=1587113"),
    ("Deadline Escape Rooms",                     "https://fareharbor.com/embeds/book/deadlineescape/?full-items=yes"),
    ("Keystone Escape Games",                     KEYSTONE_URL),
]


_sem: asyncio.Semaphore | None = None


async def run(context, target_date, fn, *args, **kwargs) -> list[dict]:
    """Spin up a fresh page, run a scraper, close the page. Respects semaphore if set."""
    async def _execute():
        page = await context.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        try:
            return await fn(page, target_date, *args, **kwargs)
        except Exception as e:
            return [{"room": "ERROR", "status": str(e)[:60], "times": []}]
        finally:
            await page.close()

    if _sem:
        async with _sem:
            return await _execute()
    return await _execute()


SCRAPERS = [
    (scrape_checkfront, ("puzzleroom",),           {"category_id": "7"}),
    (scrape_checkfront, ("breakthroughrenoesca",),  {"category_id": "5"}),
    (scrape_checkfront, ("sensology",),             {"category_id": "7"}),
    (scrape_fareharbor, ("keyandcode",),            {"flow": "1587088"}),
    (scrape_fareharbor, ("keyandcode",),            {"flow": "1587108"}),
    (scrape_fareharbor, ("keyandcode",),            {"flow": "1587113"}),
    (scrape_fareharbor, ("deadlineescape",),        {}),
    (scrape_bookeo,     (KEYSTONE_URL,),            {}),
]

VENUES = [
    ("Puzzle Room Reno",                         "https://puzzleroom.checkfront.com/reserve/"),
    ("Break Through Reno (GSR)",                 "https://gsr.breakthroughreno.com/book-now"),
    ("Sensology",                                 "https://sensology.checkfront.com/reserve/"),
    ("Key & Code — Sparks / Outlets at Legends",  "https://fareharbor.com/embeds/book/keyandcode/?full-items=yes&flow=1587088"),
    ("Key & Code — South Reno / Summit Mall",     "https://fareharbor.com/embeds/book/keyandcode/?full-items=yes&flow=1587108"),
    ("Key & Code — Reno / Costco Center",         "https://fareharbor.com/embeds/book/keyandcode/?full-items=yes&flow=1587113"),
    ("Deadline Escape Rooms",                     "https://fareharbor.com/embeds/book/deadlineescape/?full-items=yes"),
    ("Keystone Escape Games",                     KEYSTONE_URL),
]

async def main():
    global _sem

    parser = argparse.ArgumentParser(description="Reno/Sparks escape room availability checker")
    parser.add_argument(
        "--parallel", action="store_true",
        help=f"Scrape all venues concurrently (max {MAX_PARALLEL} of {os.cpu_count()} cores)",
    )
    parser.add_argument(
        "--headless", action="store_true", default=True,
        help="Run browser in headless mode (default: True)",
    )
    parser.add_argument(
        "--no-headless", action="store_false", dest="headless",
        help="Run browser in headful mode",
    )
    parser.add_argument(
        "--date", type=str,
        help="Check availability for a specific date (YYYY-MM-DD). Defaults to today.",
    )
    args = parser.parse_args()

    check_date = date.today()
    if args.date:
        try:
            check_date = datetime.datetime.strptime(args.date, "%Y-%m-%d").date()
        except ValueError:
            console.print(f"[bold red]Error:[/bold red] Invalid date format. Use YYYY-MM-DD.")
            return

    ds = get_date_strings(check_date)

    mode = f"parallel (≤{MAX_PARALLEL} cores)" if args.parallel else "sequential"
    h_mode = "headless" if args.headless else "headful"
    console.print(f"\n[dim]Date: {ds['label']} | Mode: {mode} | {h_mode}[/dim]")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=args.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )

        if args.parallel:
            _sem = asyncio.Semaphore(MAX_PARALLEL)
            results = await asyncio.gather(
                *[run(context, check_date, fn, *a, **kw) for fn, a, kw in SCRAPERS]
            )
        else:
            results = []
            for fn, a, kw in SCRAPERS:
                results.append(await run(context, check_date, fn, *a, **kw))

        await browser.close()

    print_all_venues(
        check_date,
        VENUES,
        results,
        manual=[
            ("Brainy Actz Escape Rooms", "Meadowood Mall location — call to reserve.", "(775) 225-2320"),
            ("Escape 36 (Carson City)", "No automated booking — call or visit site.", "(775) 434-7774"),
            ("Virginia City Escape Room", "Virginia City location — call to reserve.", "(775) 434-3151"),
        ],
    )

    console.print(f"\n[dim]Checked: {ds['label']}[/dim]\n")


if __name__ == "__main__":
    asyncio.run(main())
