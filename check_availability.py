#!/usr/bin/env python3
"""Check escape room availability and time slots across Reno/Sparks venues."""

import argparse
import asyncio
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

TARGET_DATE = date.today()
DATE_STR = TARGET_DATE.strftime("%Y%m%d")      # 20260418
DATE_ISO = TARGET_DATE.strftime("%Y-%m-%d")    # 2026-04-18
DATE_LABEL = TARGET_DATE.strftime("%B %-d, %Y")


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
            "Photos", "Close Continue", "Available", "Sold out", "Unavailable"}


def parse_checkfront_listing(text: str) -> list[tuple[str, str]]:
    """Return ordered [(room_name, status)] from a Checkfront listing page."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    results: list[tuple[str, str]] = []
    for i, line in enumerate(lines):
        if line in ("AVAILABLE", "SOLD OUT"):
            status = line
            for j in range(i + 1, min(i + 12, len(lines))):
                if lines[j] == "Availability" and j + 1 < len(lines):
                    candidate = lines[j + 1]
                    if candidate not in _CF_SKIP and not candidate.startswith("$") and len(candidate) > 2:
                        results.append((candidate, status))
                    break
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

async def scrape_checkfront(page: Page, subdomain: str, category_id: str = None) -> list[dict]:
    params = f"?D={DATE_STR}"
    if category_id:
        params += f"&category_id={category_id}"
    url = f"https://{subdomain}.checkfront.com/reserve/{params}"

    await goto(page, url)
    try:
        await page.wait_for_selector("[class*='item']", timeout=8000)
    except Exception:
        pass

    page_text = await page.evaluate("() => document.body.innerText")
    listing = parse_checkfront_listing(page_text)

    avail_buttons = await page.query_selector_all("a:has-text('Availability')")
    results = []

    for i in range(len(avail_buttons)):
        room, status = listing[i] if i < len(listing) else (f"Room {i + 1}", "UNKNOWN")

        if status == "SOLD OUT":
            results.append({"room": room, "status": "SOLD OUT", "times": []})
            continue

        await goto(page, url)
        try:
            await page.wait_for_selector("[class*='item']", timeout=8000)
        except Exception:
            pass

        btns = await page.query_selector_all("a:has-text('Availability')")
        if i >= len(btns):
            break

        await btns[i].click()
        await page.wait_for_timeout(1500)

        day_links = await page.query_selector_all(f"a[href*='#D{DATE_STR}']")
        if day_links:
            await day_links[-1].click()
            await page.wait_for_timeout(2000)

        modal_text = await page.evaluate("() => document.body.innerText")
        times = [l.strip() for l in modal_text.splitlines() if is_time(l)]
        results.append({"room": room, "status": status, "times": times})

    return results or [{"room": "—", "status": "NO DATA", "times": []}]


# ── FareHarbor scraper ────────────────────────────────────────────────────────

async def scrape_fareharbor(page: Page, shortname: str, flow: str = None) -> list[dict]:
    url = f"https://fareharbor.com/embeds/book/{shortname}/?full-items=yes"
    if flow:
        url += f"&flow={flow}"
    await goto(page, url, timeout=25000)

    select_btns = await page.query_selector_all("button:has-text('Select date'), a:has-text('Select date')")
    if not select_btns:
        return [{"room": "—", "status": "ERROR", "times": []}]

    results = []
    for i in range(len(select_btns)):
        await goto(page, url, timeout=25000)
        btns = await page.query_selector_all("button:has-text('Select date'), a:has-text('Select date')")
        if i >= len(btns):
            break

        # Get room name from item card
        room_name = await page.evaluate(f"""() => {{
            const btns = [...document.querySelectorAll('button, a')].filter(b => b.innerText.trim() === 'Select date');
            const btn = btns[{i}];
            if (!btn) return 'Unknown';
            let el = btn;
            for (let j = 0; j < 10; j++) {{
                el = el.parentElement;
                if (!el) break;
                const h = el.querySelector('h2, h3, h4, [class*="title"], [class*="name"]');
                if (h) return h.innerText.trim();
            }}
            return 'Unknown';
        }}""")

        await btns[i].click(force=True)
        await page.wait_for_timeout(2000)

        today_cls = await page.evaluate(f"""() => {{
            for (const td of document.querySelectorAll('td')) {{
                const btn = td.querySelector('button');
                if (btn && btn.innerText.trim() === '{TARGET_DATE.day}') return btn.className || 'found';
            }}
            return 'not_found';
        }}""")

        if "empty" in today_cls or "past-day" in today_cls:
            results.append({"room": room_name, "status": "NO AVAILABILITY TODAY", "times": []})
            continue

        clicked = await page.evaluate(f"""() => {{
            for (const td of document.querySelectorAll('td')) {{
                const btn = td.querySelector('button');
                if (btn && btn.innerText.trim() === '{TARGET_DATE.day}' && !btn.disabled && !btn.className.includes('empty') && !btn.className.includes('past-day')) {{
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
        raw_times = [l.strip() for l in text.splitlines() if is_time(l.strip())]

        # FareHarbor slots include room name: "7:15 PM Room Name | Location"
        # Extract just the time part and use room name from slot if DOM lookup failed
        slot_pattern = re.compile(r"^(\d{1,2}:\d{2}\s*(?:AM|PM))\s+(.+?)(?:\s*\|.*)?$")
        times = []
        parsed_room = room_name
        for t in raw_times:
            m = slot_pattern.match(t)
            if m:
                times.append(m.group(1).strip())
                if parsed_room in ("Unknown", "") and m.group(2).strip():
                    parsed_room = re.sub(r"\s*\|.*$", "", m.group(2)).strip()
            else:
                times.append(t)

        if parsed_room != room_name:
            room_name = parsed_room

        status = "AVAILABLE" if times else "NO AVAILABILITY TODAY"
        results.append({"room": room_name, "status": status, "times": times})

    return results or [{"room": "—", "status": "NO DATA", "times": []}]


# ── Print helpers ─────────────────────────────────────────────────────────────

STATUS_STYLE = {
    "AVAILABLE": "bold green",
    "SOLD OUT": "bold red",
    "NO AVAILABILITY TODAY": "yellow",
}


def print_venue(venue_name: str, booking_url: str, rooms: list[dict]):
    table = Table(
        title=f"[bold]{venue_name}[/bold]",
        title_justify="left",
        box=box.SIMPLE_HEAVY,
        show_lines=False,
        expand=False,
        min_width=72,
    )
    table.add_column("Room", style="white", no_wrap=True, min_width=30)
    table.add_column("Status", no_wrap=True, min_width=22)
    table.add_column("Time Slots")

    for r in rooms:
        status_style = STATUS_STYLE.get(r["status"], "white")
        status_cell = Text(r["status"], style=status_style)

        if r["times"]:
            slots = Text()
            for i, t in enumerate(r["times"]):
                if i:
                    slots.append("  ")
                slots.append(t, style=f"link {booking_url}")
            time_cell = slots
        else:
            time_cell = Text("—", style="dim")

        table.add_row(r["room"], status_cell, time_cell)

    console.print()
    console.print(table)


def print_manual(venue_name: str, note: str, phone: str = None):
    table = Table(
        title=f"[bold]{venue_name}[/bold]",
        title_justify="left",
        box=box.SIMPLE_HEAVY,
        show_lines=False,
        expand=False,
        min_width=72,
    )
    table.add_column("Note")
    cell = Text(note, style="dim italic")
    if phone:
        cell.append("  ")
        tel = "tel:" + re.sub(r"[^\d+]", "", phone)
        cell.append(phone, style=f"link {tel}")
    table.add_row(cell)
    console.print()
    console.print(table)


# ── Bookeo scraper ────────────────────────────────────────────────────────────

async def scrape_bookeo(page: Page, url: str) -> list[dict]:
    """Returns list of {room, status, times} for a Bookeo venue (requires non-headless)."""
    await goto(page, url, timeout=30000)
    # Wait for room listing to render (JS-heavy under parallel load)
    try:
        await page.wait_for_selector("text=1 hour", timeout=15000)
    except Exception:
        pass

    text = await page.evaluate("() => document.body.innerText")
    if "verify you're not a robot" in text:
        return [{"room": "—", "status": "BOT CHECK — run non-headless", "times": []}]

    lines = [l.strip() for l in text.splitlines() if l.strip()]

    rooms: dict[str, dict] = {}
    current_room = None
    # Bookeo uses "Sat, April 18, 2026" style — try both %-d and %d variants
    date_labels = {
        TARGET_DATE.strftime("%a, %B %-d, %Y"),
        TARGET_DATE.strftime("%a, %B %d, %Y"),
    }

    i = 0
    while i < len(lines):
        line = lines[i]
        if i + 1 < len(lines) and lines[i + 1] in ("1 hour", "60 min", "1 Hour"):
            current_room = line
            rooms.setdefault(current_room, {"status": "AVAILABLE", "times": []})
        elif line in date_labels and current_room:
            j = i + 1
            while j < len(lines) and is_time(lines[j]):
                rooms[current_room]["times"].append(lines[j])
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

KEYSTONE_URL = "https://www-1577h.bookeo.com/bookeo/b_keystoneescapegames_start.html"

MAX_PARALLEL = max(1, math.floor((os.cpu_count() or 4) * 0.6))

VENUES = [
    ("Puzzle Room Reno",                         "https://puzzleroom.checkfront.com/reserve/"),
    ("Break Through Reno",                        "https://gsr.breakthroughreno.com/book-now"),
    ("Sensology",                                 "https://sensologyreno.com/book-now"),
    ("Key & Code — Sparks / Outlets at Legends",  "https://fareharbor.com/embeds/book/keyandcode/?full-items=yes&flow=1587088"),
    ("Key & Code — South Reno / Summit Mall",     "https://fareharbor.com/embeds/book/keyandcode/?full-items=yes&flow=1587108"),
    ("Key & Code — Reno / Costco Center",         "https://fareharbor.com/embeds/book/keyandcode/?full-items=yes&flow=1587113"),
    ("Deadline Escape Rooms",                     "https://fareharbor.com/embeds/book/deadlineescape/?full-items=yes"),
    ("Keystone Escape Games",                     KEYSTONE_URL),
]


_sem: asyncio.Semaphore | None = None


async def run(context, fn, *args, **kwargs) -> list[dict]:
    """Spin up a fresh page, run a scraper, close the page. Respects semaphore if set."""
    async def _execute():
        page = await context.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        try:
            return await fn(page, *args, **kwargs)
        except Exception as e:
            return [{"room": "ERROR", "status": str(e)[:60], "times": []}]
        finally:
            await page.close()

    if _sem:
        async with _sem:
            return await _execute()
    return await _execute()


SCRAPERS = [
    (scrape_checkfront, ("puzzleroom",),           {}),
    (scrape_checkfront, ("breakthroughrenoesca",),  {"category_id": "5"}),
    (scrape_checkfront, ("sensology",),             {}),
    (scrape_fareharbor, ("keyandcode",),            {"flow": "1587088"}),
    (scrape_fareharbor, ("keyandcode",),            {"flow": "1587108"}),
    (scrape_fareharbor, ("keyandcode",),            {"flow": "1587113"}),
    (scrape_fareharbor, ("deadlineescape",),        {}),
    (scrape_bookeo,     (KEYSTONE_URL,),            {}),
]


async def main():
    global _sem

    parser = argparse.ArgumentParser(description="Reno/Sparks escape room availability checker")
    parser.add_argument(
        "--parallel", action="store_true",
        help=f"Scrape all venues concurrently (max {MAX_PARALLEL} of {os.cpu_count()} cores)",
    )
    args = parser.parse_args()

    mode = f"parallel (≤{MAX_PARALLEL} cores)" if args.parallel else "sequential"
    console.print(f"\n[bold]Reno / Sparks Escape Room Availability — {DATE_LABEL}[/bold]")
    console.print(f"[dim]Mode: {mode}[/dim]")

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
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
                *[run(context, fn, *a, **kw) for fn, a, kw in SCRAPERS]
            )
        else:
            results = []
            for fn, a, kw in SCRAPERS:
                results.append(await run(context, fn, *a, **kw))

        await browser.close()

    for (name, url), rooms in zip(VENUES, results):
        print_venue(name, url, rooms)

    print_manual(
        "Brainy Actz Escape Rooms",
        "No online booking for rooms — call to reserve.",
        phone="(775) 225-2320",
    )

    console.print(f"\n[dim]Checked: {DATE_LABEL}[/dim]\n")


if __name__ == "__main__":
    asyncio.run(main())
