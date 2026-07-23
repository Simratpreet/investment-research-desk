"""Core scrape → extract → S3 upload, adapted from screener_scraper/scraper.py.

Given a symbol, fetch its screener.in consolidated page, download the most
recent transcript PDFs (and optionally the latest annual report), extract text
with pypdf, and upload each non-empty result to S3 as <SYMBOL>/<name>.txt —
exactly the key layout voice_module.load_context reads.
"""

import io
import os
import re

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

S3_BUCKET = os.getenv("VOICE_S3_BUCKET", "")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
MIN_TEXT_CHARS = 200          # skip empty/scanned PDFs (matches extract_text.py)
HTTP_TIMEOUT = 60
SYMBOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,19}$")

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_s3_client = None


def _s3():
    global _s3_client
    if _s3_client is None:
        import boto3
        _s3_client = boto3.client("s3", region_name=AWS_REGION)
    return _s3_client


def valid_symbol(symbol: str) -> bool:
    return bool(symbol) and bool(SYMBOL_RE.match(symbol)) and ".." not in symbol


def _discover_links(symbol: str):
    """Return (transcript_urls, ppt_urls, annual_report_url|None) from the page."""
    url = f"https://www.screener.in/company/{symbol}/consolidated/"
    resp = requests.get(url, headers=_HEADERS, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.content, "html.parser")

    transcripts, ppts = [], []
    for link in soup.find_all("a", class_="concall-link"):
        href = link.get("href")
        if not href:
            continue
        full = href if href.startswith("http") else "https://www.screener.in" + href
        text = link.get_text(strip=True).lower()
        if "transcript" in text:
            transcripts.append(full)
        elif "ppt" in text or "presentation" in text:
            ppts.append(full)

    annual = None
    ar = soup.find_all("a", class_=lambda c: c and "plausible-event-name=Annual+Report" in c)
    if ar and ar[0].get("href"):
        href = ar[0]["href"]
        annual = href if href.startswith("http") else "https://www.screener.in" + href
    return transcripts, ppts, annual


def _pdf_to_text(pdf_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf_bytes))
    return "\n".join((p.extract_text() or "") for p in reader.pages).strip()


def _upload_txt(symbol: str, name: str, text: str):
    key = f"{symbol}/{name}.txt"
    _s3().put_object(Bucket=S3_BUCKET, Key=key,
                     Body=text.encode("utf-8"), ContentType="text/plain; charset=utf-8")
    return key


def _key_exists(symbol: str, name: str) -> bool:
    try:
        _s3().head_object(Bucket=S3_BUCKET, Key=f"{symbol}/{name}.txt")
        return True
    except Exception:
        return False


def scrape_symbol(symbol: str, transcripts: int = 2, ppts: int = 1,
                  annual: bool = True, force: bool = False) -> dict:
    """Download + extract + upload filings for one symbol. Returns a summary."""
    if not S3_BUCKET:
        raise RuntimeError("VOICE_S3_BUCKET not configured")
    sym = symbol.strip().upper()
    if not valid_symbol(sym):
        raise ValueError("invalid symbol")

    tr_urls, ppt_urls, ar_url = _discover_links(sym)
    uploaded, skipped, errors = [], [], []

    jobs = [(f"Transcript_{i+1}", u) for i, u in enumerate(tr_urls[:max(0, transcripts)])]
    jobs += [(f"PPT_{i+1}", u) for i, u in enumerate(ppt_urls[:max(0, ppts)])]
    if annual and ar_url:
        jobs.append(("AnnualReport_1", ar_url))

    for name, pdf_url in jobs:
        if not force and _key_exists(sym, name):
            skipped.append(f"{sym}/{name}.txt (exists)")
            continue
        try:
            r = requests.get(pdf_url, headers=_HEADERS, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            text = _pdf_to_text(r.content)
            if len(text) < MIN_TEXT_CHARS:
                skipped.append(f"{sym}/{name} (empty/scanned)")
                continue
            uploaded.append(_upload_txt(sym, name, text))
        except Exception as e:
            errors.append(f"{name}: {str(e)[:120]}")

    return {"symbol": sym, "transcripts_found": len(tr_urls),
            "ppts_found": len(ppt_urls),
            "uploaded": uploaded, "skipped": skipped, "errors": errors}
