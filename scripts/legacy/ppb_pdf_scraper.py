import os
import time
import csv 
import requests
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
import fitz  # PyMuPDF
import pandas as pd
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed


BASE_PAGES = [
    'https://web.pharmacyboardkenya.org/hpt-guidelines/',
    'https://web.pharmacyboardkenya.org/clinical-trials-guidelines/',
    'https://web.pharmacyboardkenya.org/good-distribution-practice/',
    'https://web.pharmacyboardkenya.org/post-market-surveillance-guidelines/',
    'https://web.pharmacyboardkenya.org/pharmacy-practice-guidelines/',
    'https://web.pharmacyboardkenya.org/quality-control-and-lot-release-guidelines/',
    'https://web.pharmacyboardkenya.org/trade-affairs/',
    'https://web.pharmacyboardkenya.org/ports-of-entry-guidelines/',
    'https://web.pharmacyboardkenya.org/download/new-premise-application-approval-process/',
    'https://web.pharmacyboardkenya.org/download/marketing-authorisation-pathways/',
    'https://web.pharmacyboardkenya.org/download/license-renewal-and-variation-process-flow/',
    'https://web.pharmacyboardkenya.org/download/pharmacy-and-poisons-rules/',
]

OUTPUT_DIR = os.path.join('data', 'ppb_pdfs')
CSV_FILE = 'ppb_pdf_index.csv'
USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3'
HEADERS = {'User-Agent': USER_AGENT}
DELAY_BETWEEN_REQUESTS = 2  # seconds
TIMEOUT = 30  # seconds
MAX_THREADS = 5  # for subpage crawling

os.makedirs(OUTPUT_DIR, exist_ok=True)

session = requests.session()
session.headers.update(HEADERS)
session.timeout = TIMEOUT


def get_page(url):
    resp = session.get(url, timeout=TIMEOUT)
    resp.raise_for_status()
    time.sleep(DELAY_BETWEEN_REQUESTS)
    return resp.text


def crawl_subpage_for_pdfs(subpage_url):
    """Crawl a single subpage (multithreaded) for PDF links."""
    pdf_links = []
    try:
        sub_html = get_page(subpage_url)
        sub_soup = BeautifulSoup(sub_html, 'html.parser')
        for sub_a in sub_soup.find_all('a', href=True):
            sub_href = sub_a['href']
            if sub_href.lower().endswith('.pdf'):
                pdf_url = urljoin(subpage_url, sub_href)
                pdf_links.append((sub_a.get_text(strip=True), pdf_url))
    except Exception as e:
        print(f"Error fetching subpage {subpage_url}: {e}")
    return pdf_links


def find_pdf_links(html, base_url):
    soup = BeautifulSoup(html, 'html.parser')
    links = []
    subpages = []

    for a in soup.find_all('a', href=True):
        href = a['href']
        full_url = urljoin(base_url, href)

        if href.lower().endswith('.pdf') or ".pdf?" in href.lower():
            links.append((a.get_text(strip=True), full_url))
        elif '/download/' in href.lower():
            subpages.append(full_url)

    # Multithreaded subpage crawling
    if subpages:
        with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
            futures = {executor.submit(crawl_subpage_for_pdfs, u): u for u in subpages}
            for fut in tqdm(as_completed(futures), total=len(futures), desc="Subpages", leave=False):
                links.extend(fut.result())

    return sorted(links)

import hashlib 
def safe_filename_from_url(url, index=None):
    """Generate a safe filename from URL, using index if provided."""
    parsed = urlparse(url)
    base = os.path.basename(parsed.path)

    if not base or base.lower().startswith("download") or not base.lower().endswith(('.pdf')):
        if index is not None:
            name_part = str(index) 
        else:
            name_part = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
        
        return f"ppb_{name_part}.pdf"
    
    if not base.lower().endswith('.pdf'):
        base += '.pdf'

    return base 


def download_file(url, dest_path, category=None):

    if category:
        sub_folder = os.path.join(os.path.dirname(dest_path), category)
        os.makedirs(sub_folder, exist_ok=True)
        dest_path = os.path.join(sub_folder, os.path.basename(dest_path))

    r = session.get(url, stream=True, timeout=TIMEOUT)
    r.raise_for_status()
    total = int(r.headers.get("content-length", 0))
    downloaded = 0

    with open(dest_path, "wb") as f, tqdm(
        total=total,
        unit='B',
        unit_scale=True,
        desc=os.path.basename(dest_path),
        leave=False
    ) as pbar:
        for chunk in r.iter_content(chunk_size=8192):
            if chunk:
                f.write(chunk)
                downloaded += len(chunk)
                pbar.update(len(chunk))

    if not os.path.exists(dest_path) or os.path.getsize(dest_path) == 0:
        raise IOError("Downloaded file is empty or does not exist.")
    
    return os.path.getsize(dest_path), total


def extract_pdf_text_and_meta(path):
    data = {"file_path": path, "num_pages": None, "text_snippet": "", "full_text": ""}

    if not os.path.exists(path):
        data["error"] = "File does not exist."
        print(f"Skipping missing file:{path}")
        return data
    
    try:
        with fitz.open(path) as pdf:
            data["num_pages"] = pdf.page_count
            texts = []
            for i in range(min(4, pdf.page_count)):
                page = pdf[i]
                texts.append(page.get_text("text") or "")
            snippet = "\n".join(texts).strip()
            data["text_snippet"] = snippet[:2000]  # first 2k characters
    except Exception as e:
        data["error"] = str(e)
        print(f"Error processing {path}: {e}")
    return data


def load_existing_index():
    """Load previously scraped CSV if available for resume-safe runs."""
    if os.path.exists(CSV_FILE):
        return pd.read_csv(CSV_FILE)
    return pd.DataFrame(columns=["url", "file_name", "local_path", "status"])


def scrape_download_page(start_url, max_pdfs=None):
    html = get_page(start_url)
    links = find_pdf_links(html, start_url)
    print(f"\nFound {len(links)} PDF links on {start_url}\n")

    existing_df = load_existing_index()
    downloaded_urls = set(existing_df['url'].tolist())

    rows = []
    for i, (link_text, pdf_url) in enumerate(tqdm(links, desc="Processing PDFs", unit="file")):
        if max_pdfs and i >= max_pdfs:
            break

        fname = safe_filename_from_url(pdf_url, i + 1)
        dest = os.path.join(OUTPUT_DIR, fname)
        os.makedirs(os.path.dirname(dest), exist_ok=True) # ensure output dir exists 

        # Skip if already downloaded (resume)
        if pdf_url in downloaded_urls and os.path.exists(dest):
            print(f"Skipping already downloaded: {fname}")
            continue

        #---CATEGORIZATION LOGIC---
        category = "OTHER"
        lower_text = (link_text or "").lower()
        lower_url = pdf_url.lower()

        if "guideline" in lower_url or "guidelines" in lower_text:
            category = "GUIDELINES"
        elif "policy" in lower_url or "framework" in lower_text:
            category = "POLICIES"
        elif "product" in lower_url or "registration" in lower_text:
            category = "REGISTRATION"
        elif "form" in lower_url or "application" in lower_text:
            category = "APPLICATIONS"
        elif "training" or "manual" in lower_text:
            category = "TRAINING"
        elif "report" in lower_url or "survey" in lower_text:
            category = "REPORTS"
        elif "inspection" in lower_url or "audit" in lower_text:
            category = "INSPECTION"
        
        category_dir = os.path.join(OUTPUT_DIR, category)
        os.makedirs(category_dir, exist_ok=True)
        dest = os.path.join(category_dir, fname)

        print(f"Downloading ({i+1}/{len(links)}): {fname} -> {category} (dest: {dest})")
        try:
            size, reported = download_file(pdf_url, dest, category=None)
            file_size = size
        except Exception as e:
            print("Download failed:", e)
            rows.append({"url": pdf_url, "file_name": fname, "status": "download_failed", "error": str(e)})
            time.sleep(DELAY_BETWEEN_REQUESTS)
            continue

        meta = extract_pdf_text_and_meta(dest)
        rows.append({
            "url": pdf_url,
            "file_name": fname,
            "local_path": dest,
            "file_size": file_size,
            "num_pages": meta.get("num_pages"),
            "text_snippet": meta.get("text_snippet"),
            "status": "downloaded" if os.path.exists(dest) else "failed",
            "error": meta.get("error", "")
        })
        time.sleep(DELAY_BETWEEN_REQUESTS)

    # Merge with existing data
    df_new = pd.DataFrame(rows)
    df_final = pd.concat([existing_df, df_new], ignore_index=True).drop_duplicates(subset=["url"], keep="last")
    df_final.to_csv(CSV_FILE, index=False)
    print(f"Saved index to {CSV_FILE}")
    return df_final


if __name__ == "__main__":
    all_rows = []
    for page in tqdm(BASE_PAGES, desc="Scraping all guideline pages"):
        assert urlparse(page).scheme in ('http', 'https'), f"Invalid URL: {page}"
        df = scrape_download_page(page)
        all_rows.append(df)

    if all_rows:
        df_all = pd.concat(all_rows, ignore_index=True)
        print(df_all.head())
        print(f"\nTotal PDFs processed: {len(df_all)}\n") 

            # --- Summary by subfolder/category ---
    from collections import Counter

    print("\nDownload Summary by Category:\n")

    total = 0
    for category in os.listdir(OUTPUT_DIR):
        category_path = os.path.join(OUTPUT_DIR, category)
        if os.path.isdir(category_path):
            pdf_count = len([
                f for f in os.listdir(category_path)
                if f.lower().endswith(".pdf")
            ])
            total += pdf_count
            print(f"{category}: {pdf_count} PDFs")

    print(f"\n Total PDFs downloaded across all categories: {total}")

