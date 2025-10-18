# app.py
import io
import os
import zipfile
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, send_file, flash, redirect, url_for

# Optional Selenium fallback
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options as ChromeOptions
from webdriver_manager.chrome import ChromeDriverManager

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "change-me-for-prod")

# --- Configuration ---
BASE_PAGE_URL = "https://newdelhi.dcourts.gov.in/cause-list-%e2%81%84-daily-board/"
REQUEST_TIMEOUT = 20  # seconds

# --- Helpers ---------------------------------------------------------------

def absolute_url(base, link):
    return urljoin(base, link)

def looks_like_pdf_link(href, text, date_strs):
    """
    Heuristics to decide whether an <a href> is likely a judge causelist PDF for the chosen date.
    - If href ends with .pdf => likely.
    - If href contains 'cause'/'causelist'/'board' etc => likely.
    - If the href or link text contains the date in any provided string format => likely.
    """
    if not href:
        return False
    lower = href.lower()
    text_lower = (text or "").lower()

    # direct PDF
    if lower.endswith(".pdf"):
        return True

    # keywords
    keywords = ["cause", "causelist", "cause-list", "daily-board", "board", "causel"]
    if any(k in lower for k in keywords) or any(k in text_lower for k in keywords):
        return True

    # date matching
    for d in date_strs:
        if d in lower or d in text_lower:
            return True

    return False

def generate_date_variations(date_iso):
    """
    Input: 'YYYY-MM-DD'
    Return list of date string patterns to match in links/text:
    - 'YYYY-MM-DD', 'DD-MM-YYYY', 'DD/MM/YYYY', 'DDMMYYYY', 'YYYYMMDD'
    - month name forms: 'DD-MMM-YYYY', 'DD-MMM-YY' (lowercase)
    """
    y, m, d = date_iso.split("-")
    dd = d
    mm = m
    yyyy = y
    ddmmyyyy = f"{dd}{mm}{yyyy}"
    yyyymmdd = f"{yyyy}{mm}{dd}"
    dmy_dash = f"{dd}-{mm}-{yyyy}"
    dmy_slash = f"{dd}/{mm}/{yyyy}"
    # short month name
    import calendar
    mname = calendar.month_abbr[int(mm)].lower()  # e.g., 'Oct' -> 'Oct'
    dmy_mon = f"{dd}-{mname}-{yyyy}"
    return list({date_iso, ddmmyyyy, yyyymmdd, dmy_dash, dmy_slash, dmy_mon})

def fetch_page_with_requests(url):
    resp = requests.get(url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    return resp.text, resp.url  # return possibly redirected URL as base

def fetch_page_with_selenium(url):
    """
    Fallback: use headless Chrome to render JS and get page source.
    Requires Chrome/Chromium in environment. webdriver-manager will download driver.
    """
    opts = ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("user-agent=Mozilla/5.0")
    driver = None
    try:
        driver = webdriver.Chrome(service=ChromeService(ChromeDriverManager().install()), options=opts)
        driver.get(url)
        # wait a brief moment for JS to populate (could be improved using expected_conditions)
        import time
        time.sleep(2)
        return driver.page_source, driver.current_url
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

def collect_candidate_pdf_links(html, base_url, date_strs):
    soup = BeautifulSoup(html, "html.parser")
    anchors = soup.find_all("a")
    candidates = []
    for a in anchors:
        href = a.get("href")
        text = a.get_text(" ", strip=True)
        if not href:
            continue
        # make absolute
        full = absolute_url(base_url, href)
        if looks_like_pdf_link(href, text, date_strs) or looks_like_pdf_link(full, text, date_strs):
            candidates.append({"href": full, "text": text})
    # dedupe preserving order
    seen = set()
    unique = []
    for c in candidates:
        if c["href"] not in seen:
            seen.add(c["href"])
            unique.append(c)
    return unique

def download_binary(session, url):
    resp = session.get(url, timeout=REQUEST_TIMEOUT, stream=True)
    resp.raise_for_status()
    return resp.content, resp.headers.get("Content-Type", "")

# --- Routes ---------------------------------------------------------------

@app.route("/", methods=["GET"])
def index():
    return render_template("index.html", base_url=BASE_PAGE_URL)

@app.route("/download_all", methods=["POST"])
def download_all():
    date = request.form.get("date")
    if not date:
        flash("Please provide a date in YYYY-MM-DD format.", "error")
        return redirect(url_for("index"))

    # create date patterns
    date_variants = generate_date_variations(date)

    # 1) Try with requests
    try:
        html, final_base = fetch_page_with_requests(BASE_PAGE_URL)
    except Exception as e_req:
        # fallback to Selenium on request failure
        try:
            html, final_base = fetch_page_with_selenium(BASE_PAGE_URL)
        except Exception as e_se:
            return f"<h1>Error</h1><p>Failed to fetch the court page with requests and selenium.<br>Requests error: {e_req}<br>Selenium error: {e_se}</p>", 500

    # 2) Find candidate links
    candidates = collect_candidate_pdf_links(html, final_base, date_variants)

    # If nothing candidate found, fallback to selenium render and re-check
    if not candidates:
        try:
            html_js, final_base_js = fetch_page_with_selenium(BASE_PAGE_URL)
            candidates = collect_candidate_pdf_links(html_js, final_base_js, date_variants)
        except Exception:
            pass

    if not candidates:
        # No candidates found — return informative message to tweak heuristics
        return (
            "<h1>No cause-list links found</h1>"
            "<p>Could not find PDF links matching heuristics on the page. "
            "This site may use a different structure. You can:</p>"
            "<ul>"
            "<li>Open the page and inspect link patterns, then share them.</li>"
            "<li>I can adjust the script to match exact selectors (e.g., a specific CSS class or JS API)</li>"
            "</ul>"
        ), 404

    # 3) Download candidates and compress into zip
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    memory_zip = io.BytesIO()
    with zipfile.ZipFile(memory_zip, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for idx, c in enumerate(candidates, start=1):
            try:
                content, content_type = download_binary(session, c["href"])
            except Exception as e:
                # skip this file but continue
                continue
            # determine filename
            parsed = urlparse(c["href"])
            basename = os.path.basename(parsed.path) or f"causelist_{idx}.pdf"
            if not basename.lower().endswith(".pdf"):
                # ensure .pdf extension
                basename = f"{basename}.pdf"
            # if duplicate name, make unique
            arcname = basename
            count = 1
            while arcname in zf.namelist():
                arcname = f"{os.path.splitext(basename)[0]}_{count}.pdf"
                count += 1
            zf.writestr(arcname, content)

    memory_zip.seek(0)
    zip_name = f"cause_lists_{date}.zip"
    return send_file(memory_zip, as_attachment=True, download_name=zip_name, mimetype="application/zip")

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
