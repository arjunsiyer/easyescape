import streamlit as st
import asyncio
import datetime
from datetime import date
import subprocess
import os
import pandas as pd
import math
import multiprocessing

# Ensure playwright browsers are installed (needed for Streamlit Cloud)
@st.cache_resource
def install_playwright():
    subprocess.run(["playwright", "install", "chromium"])

install_playwright()

from playwright.async_api import async_playwright
import check_availability as scraper

# Dynamic concurrency limit for Streamlit Cloud (approx 1 core per 1GB RAM)
# We aim for roughly 1.5 parallel browsers per core, with a floor of 2.
CPU_COUNT = multiprocessing.cpu_count()
MAX_CONCURRENCY = max(2, math.floor(CPU_COUNT * 1.5))

st.set_page_config(
    page_title="Reno Escape Finder",
    page_icon="🔓",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# Bespoke CSS for a high-fidelity "Cyber-Terminal" aesthetic
st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Orbitron:wght@400;900&display=swap');

    /* Overall page background with scanline effect */
    .stApp {
        background: radial-gradient(circle at center, #0a0e14 0%, #05070a 100%);
        color: #00ffcc; /* Cyber Cyan */
        font-family: 'JetBrains Mono', monospace;
    }
    
    .stApp::before {
        content: " ";
        display: block;
        position: absolute;
        top: 0; left: 0; bottom: 0; right: 0;
        background: linear-gradient(rgba(18, 16, 16, 0) 50%, rgba(0, 0, 0, 0.25) 50%), linear-gradient(90deg, rgba(255, 0, 0, 0.06), rgba(0, 255, 0, 0.02), rgba(0, 0, 255, 0.06));
        z-index: 100;
        background-size: 100% 2px, 3px 100%;
        pointer-events: none;
    }

    /* Centered Container */
    .main .block-container {
        padding-top: 2rem;
        max-width: 850px;
    }

    /* Cyber Title */
    .main-title {
        font-family: 'Orbitron', sans-serif;
        font-weight: 900;
        color: #00ffcc;
        text-align: center;
        text-transform: uppercase;
        letter-spacing: 5px;
        margin-bottom: 1.5rem;
        font-size: 2.2rem;
        text-shadow: 0 0 5px rgba(0, 255, 204, 0.6), 0 0 10px rgba(0, 255, 204, 0.4);
    }
    
    /* Modern Search Card with Neon Border */
    [data-testid="stElementContainer"]:has(#search-area) + div [data-testid="stVerticalBlockBorderWrapper"] > div {
        background: rgba(10, 14, 20, 0.8) !important;
        border: 2px solid #00ffcc !important;
        border-radius: 4px !important;
        padding: 1.5rem !important;
        box-shadow: 0 0 15px rgba(0, 255, 204, 0.2), inset 0 0 10px rgba(0, 255, 204, 0.1) !important;
        backdrop-filter: blur(10px) !important;
    }

    /* Tactical Date Picker */
    div[data-baseweb="input"] {
        background-color: #000 !important;
        border: 1px solid #00ffcc !important;
        border-radius: 0px !important;
    }
    
    div[data-baseweb="input"] input {
        color: #00ffcc !important;
        font-family: 'JetBrains Mono', monospace !important;
        font-size: 0.9rem !important;
    }

    /* High-Voltage Primary Button */
    .stButton > button {
        width: 100% !important;
        background: #00ffcc !important;
        color: #000 !important;
        border: none !important;
        padding: 0.8rem !important;
        font-family: 'Orbitron', sans-serif !important;
        font-weight: 900 !important;
        border-radius: 0px !important;
        transition: all 0.2s ease !important;
        text-transform: uppercase;
        letter-spacing: 2px;
        box-shadow: 0 0 20px rgba(0, 255, 204, 0.4) !important;
        font-size: 1rem !important;
    }
    
    .stButton > button:hover {
        background: #ff00ff !important;
        color: #fff !important;
        box-shadow: 0 0 30px rgba(255, 0, 255, 0.6) !important;
    }

    /* Custom Table Styling (Integrated for Single Scroll) */
    .venue-group-header {
        background-color: rgba(0, 255, 204, 0.1);
        color: #00ffcc;
        font-weight: 800;
        padding: 10px 14px;
        margin-top: 1.5rem;
        margin-bottom: 0.5rem;
        display: flex;
        justify-content: space-between;
        align-items: center;
        border-left: 4px solid #00ffcc;
        font-family: 'Orbitron', sans-serif;
        text-transform: uppercase;
        letter-spacing: 1px;
        font-size: 0.85rem;
    }

    .results-table { width: 100%; border-collapse: separate; border-spacing: 0 6px; margin-bottom: 1.5rem; }
    .result-row { background: rgba(10, 14, 20, 0.6); border: 1px solid rgba(0, 255, 204, 0.1); }
    .result-row td { padding: 12px; color: #00ffcc; font-size: 0.85rem; }
    
    .status-pill { 
        padding: 2px 8px; 
        font-size: 0.7rem; 
        font-weight: 700; 
        text-transform: uppercase; 
        border: 1px solid currentColor;
        display: inline-block;
    }

    .time-slot {
        background: rgba(0, 255, 204, 0.05);
        color: #00ffcc;
        padding: 1px 6px;
        border: 1px solid rgba(0, 255, 204, 0.3);
        margin-right: 4px;
        font-size: 0.75rem;
        display: inline-block;
        margin-bottom: 3px;
    }

    .book-link {
        color: #ff00ff;
        text-decoration: none;
        font-weight: 900;
        font-size: 0.7rem;
        border: 1px solid #ff00ff;
        padding: 3px 10px;
        transition: all 0.2s;
        font-family: 'Orbitron', sans-serif;
    }

    .result-row.completed { opacity: 0.25; filter: grayscale(1); }
    .result-row.completed td { text-decoration: line-through; }
    
    .cyber-cb {
        appearance: none;
        width: 16px;
        height: 16px;
        border: 2px solid #00ffcc;
        background: transparent;
        cursor: pointer;
        flex-shrink: 0; /* Prevent squishing */
        margin-right: 10px;
        margin-top: 2px;
    }
    .cyber-cb:checked { background: #00ffcc; }

    /* Hide standard Streamlit header/footer */
    header, footer {visibility: hidden !important;}
    
    @media (max-width: 640px) {
        .main-title { font-size: 1.4rem; letter-spacing: 2px; }
        .result-row td { padding: 10px 8px; }
        .venue-group-header { font-size: 0.75rem; }
    }
    </style>
    """, unsafe_allow_html=True)

# Main UI Layout
st.markdown('<h1 class="main-title">🔓 RENO ESCAPE FINDER</h1>', unsafe_allow_html=True)

# Search Card
st.markdown('<div id="search-area"></div>', unsafe_allow_html=True)
with st.container(border=True):
    col1, col2 = st.columns([2, 1], gap="medium")
    with col1:
        selected_date = st.date_input("Target Date", date.today(), min_value=date.today(), label_visibility="collapsed")
    with col2:
        check_button = st.button("GO", type="primary", width="stretch")
st.markdown('<br>', unsafe_allow_html=True)

# Placeholder for results
results_container = st.empty()

# Manual entries (venues that don't support automated scraping)
MANUAL_VENUES = [
    {"Venue": "Brainy Actz Escape Rooms", "Room": "—", "Status": "Manual Only", "Time Slots": "Call (775) 225-2320"},
    {"Venue": "Escape 36 (Carson City)", "Room": "—", "Status": "Manual Only", "Time Slots": "Call (775) 434-7774"},
    {"Venue": "Virginia City Escape Room", "Room": "—", "Status": "Manual Only", "Time Slots": "Call (775) 434-3151"},
]

# Persistence: Initialize session state for results
if "search_results" not in st.session_state:
    st.session_state.search_results = None

def format_status(status):
    if status == "AVAILABLE":
        return "AVAILABLE"
    if status == "SOLD OUT":
        return "SOLD OUT"
    if "NO AVAILABILITY" in status or "NO DATA" in status:
        return "NO AVAILABILITY"
    return status

# Venue booking URLs for the "Book" button
BOOKING_URLS = {name: url for name, url in scraper.VENUES}
BOOKING_URLS.update({
    "Brainy Actz Escape Rooms": "https://brainyactz.com/locations/reno-nv/",
    "Escape 36 (Carson City)": "https://escape36.com/",
    "Virginia City Escape Room": "https://www.virginiacityescaperooms.com/"
})

async def run_scrapers(target_date):
    all_data = []
    status_text = st.status("LOADING...", expanded=True)
    
    # Use a semaphore to prevent crashing the container
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-setuid-sandbox"],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                locale="en-US",
            )

            tasks = []
            for name, _url in scraper.VENUES:
                idx = [v[0] for v in scraper.VENUES].index(name)
                fn, a, kw = scraper.SCRAPERS[idx]
                
                async def wrapped_scrape(n=name, f=fn, args=a, kwargs=kw):
                    async with semaphore: # Respect the dynamic resource limit
                        try:
                            page = await context.new_page()
                            await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
                            try:
                                res = await f(page, target_date, *args, **kwargs)
                                return n, res
                            except Exception as e:
                                return n, [{"room": "Error", "status": "Error loading", "times": []}]
                            finally:
                                # Ensure we don't try to close a page if the browser is gone
                                if browser.is_connected():
                                    await page.close()
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            return n, [{"room": "Error", "status": "Error loading", "times": []}]
                
                tasks.append(asyncio.create_task(wrapped_scrape()))

            try:
                for future in asyncio.as_completed(tasks):
                    try:
                        venue_name, room_results = await future
                        status_text.write(f"✓ {venue_name}")
                        
                        for r in room_results:
                            all_data.append({
                                "Venue": venue_name,
                                "Room": r["room"],
                                "Status": format_status(r["status"]),
                                "Time Slots": ", ".join(r["times"]) if r["times"] else "—",
                                "Link": BOOKING_URLS.get(venue_name, "#")
                            })
                        
                        # Update state and display
                        df = pd.DataFrame(all_data + MANUAL_VENUES)
                        df['is_avail'] = df['Status'].apply(lambda x: 0 if x == "AVAILABLE" else 1)
                        df = df.sort_values(['is_avail', 'Venue']).drop(columns=['is_avail'])
                        st.session_state.search_results = df
                        render_results(df)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        continue # Skip failed individual venue results in the table update

                await browser.close()
                status_text.update(label="COMPLETE", state="complete", expanded=False)
            
            except asyncio.CancelledError:
                # User interrupted (e.g. refresh or click)
                # Cleanup tasks if they are still running
                for t in tasks:
                    if not t.done():
                        t.cancel()
                if browser.is_connected():
                    await browser.close()
                raise
    
    except asyncio.CancelledError:
        raise
    except Exception as e:
        st.error(f"SYSTEM OVERHEAT: {str(e)}")

def render_results(df):
    if df is None or df.empty:
        return

    html_output = '<div class="results-wrapper">'
    
    grouped = df.groupby("Venue", sort=False)
    for venue, group in grouped:
        link = BOOKING_URLS.get(venue, "#")
        html_output += f"""
        <div class="venue-group-header">
            <span>{venue}</span>
            <a href="{link}" target="_blank" class="book-link">BOOK</a>
        </div>
        <table class="results-table">
        """
        for _, row in group.iterrows():
            status = row["Status"]
            status_class = "status-none"
            if status == "AVAILABLE": status_class = "status-available"
            elif status == "SOLD OUT": status_class = "status-soldout"
            
            room_id = "".join(filter(str.isalnum, f"{venue}{row['Room']}"))
            
            times_html = ""
            if row["Time Slots"] != "—" and "Call " not in str(row["Time Slots"]):
                slots = str(row["Time Slots"]).split(", ")
                for s in slots:
                    times_html += f'<span class="time-slot">{s}</span>'
            else:
                note = row["Time Slots"] if row["Time Slots"] != "—" else "No slots available"
                times_html = f'<span style="color: #64748b; font-style: italic; font-size: 0.75rem;">{note}</span>'

            html_output += f"""
            <tr class="result-row" id="row-{room_id}">
                <td style="width: 45%;">
                    <div style="display: flex; align-items: flex-start;">
                        <input type="checkbox" class="cyber-cb" id="cb-{room_id}" onclick="toggleComplete('{room_id}')">
                        <div style="min-width: 0;">
                            <div style="font-weight: 700; color: #fff; line-height: 1.2;">{row["Room"]}</div>
                            <div style="margin-top: 4px;"><span class="status-pill {status_class}">{status}</span></div>
                        </div>
                    </div>
                </td>
                <td style="width: 55%; vertical-align: top;">{times_html}</td>
            </tr>
            """
        html_output += "</table>"

    html_output += '</div>'
    
    # Render directly to page to ensure single scroll
    st.markdown(html_output, unsafe_allow_html=True)

# Logic to handle persistent display
if check_button:
    asyncio.run(run_scrapers(selected_date))
elif st.session_state.search_results is not None:
    render_results(st.session_state.search_results)
else:
    # Initial manual load
    df_manual = pd.DataFrame(MANUAL_VENUES)
    df_manual["Link"] = [BOOKING_URLS.get(v["Venue"], "#") for _, v in df_manual.iterrows()]
    render_results(df_manual)
