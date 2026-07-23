#!/usr/bin/env python3
"""
On-demand discovery pull: every new annual report freshly uploaded site-wide on
screener.in (NOT scoped to your watchlist — the point is to surface new names),
analyzed via OpenRouter (z-ai/glm-5.2) for: (1) management commentary/outlook, (2) any
guidance given. Run manually — no scheduling.

  ./venv/bin/python3.14 annual_reports.py

Reuses fetch/cookie/PDF/cooldown logic from scan.py rather than duplicating it.

Project files (alongside this script):
  annual_reports_seen.json    - state file of report PDF URLs already analyzed (auto-managed)
  annual_reports_digest.md    - running log of analyses (auto-managed)
"""
import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import scan

SEEN_FILE = scan.CONFIG_DIR / "annual_reports_seen.json"
DIGEST_FILE = scan.CONFIG_DIR / "annual_reports_digest.md"

ANNUAL_REPORTS_URL = "https://www.screener.in/annual-reports/"
ANNUAL_REPORT_MAX_PAGES = 60
ANNUAL_REPORT_MAX_CHARS = 60000
REPORT_FETCH_DELAY_SECONDS = 1.0
REPORT_WORKERS = 4
CHECKPOINT_EVERY = 20

LI_BLOCK_RE = re.compile(r"<li>(.*?)</li>", re.S)
PDF_LINK_RE = re.compile(r'href="([^"]+\.pdf)"')
NAME_RE = re.compile(r'<strong class="font-weight-500">\s*([^<]+?)\s*<i')
FY_RE = re.compile(r'<span class="sub font-size-14">\s*([^<]+?)\s*</span>')
CODE_RE = re.compile(r"/company/([A-Za-z0-9\.\-&]+)/")


def load_seen():
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()).get("seen", []))
        except (json.JSONDecodeError, OSError):
            return set()
    return set()


def save_seen(seen_set):
    SEEN_FILE.write_text(json.dumps({"seen": list(seen_set)}, indent=0))


def parse_annual_reports(body):
    reports = []
    for block in LI_BLOCK_RE.findall(body):
        pdf_match = PDF_LINK_RE.search(block)
        name_match = NAME_RE.search(block)
        code_match = CODE_RE.search(block)
        if not (pdf_match and name_match and code_match):
            continue
        fy_match = FY_RE.search(block)
        reports.append({
            "url": pdf_match.group(1),
            "company": name_match.group(1).strip(),
            "code": code_match.group(1),
            "fy": fy_match.group(1).strip() if fy_match else "",
        })
    return reports


def extract_report_text(url):
    """Pull text from the first ANNUAL_REPORT_MAX_PAGES pages — Indian annual reports
    almost always carry the Board's Report / MD&A section early (well before the
    financial statements), so a generous front slice reliably captures it without
    needing to parse the whole (often 100-300 page) document."""
    try:
        data = scan.fetch_bytes(url)
        reader = scan.PdfReader(scan.io.BytesIO(data))
        text_parts = []
        for page in reader.pages[:ANNUAL_REPORT_MAX_PAGES]:
            text_parts.append(page.extract_text() or "")
        text = re.sub(r"\s+", " ", " ".join(text_parts)).strip()
        return text[:ANNUAL_REPORT_MAX_CHARS] if text else None
    except Exception as e:
        scan.log(f"  [warn] extraction failed for {url}: {e}")
        return None


def analyze_with_openrouter(report, text, api_key):
    prompt = (
        f"Below is an excerpt from {report['company']}'s {report['fy']} annual report "
        f"(first ~{ANNUAL_REPORT_MAX_PAGES} pages of extracted text). Answer these two "
        "questions based only on what's in the excerpt:\n"
        "1. What is management's commentary and outlook on the business?\n"
        "2. Are they giving any specific guidance (revenue, margin, capex, growth "
        "targets, etc.) in the report?\n\n"
        "If the excerpt doesn't contain enough to answer a question, say so explicitly "
        "rather than guessing. Be concise and factual — a few sentences per question, "
        "no fluff, no disclaimers.\n\n"
        f"--- Excerpt ---\n{text}"
    )
    req_body = json.dumps({
        "model": scan.OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 16000,
    }).encode("utf-8")
    req = scan.urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=req_body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )

    RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}
    delay = scan.REQUEST_DELAY_SECONDS
    for attempt in range(1, scan.MAX_RETRIES + 1):
        try:
            with scan.urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            content = data["choices"][0]["message"].get("content")
            if not content:
                reason = data["choices"][0].get("finish_reason")
                if attempt < scan.MAX_RETRIES:
                    scan.log(f"  [retry] OpenRouter returned empty content (finish_reason={reason}), retrying in {delay:.1f}s")
                    time.sleep(delay)
                    delay *= 3
                    continue
                raise RuntimeError(f"OpenRouter returned empty content after {scan.MAX_RETRIES} attempts (finish_reason={reason})")
            return content.strip()
        except scan.urllib.error.HTTPError as e:
            if e.code in RETRYABLE_HTTP_CODES and attempt < scan.MAX_RETRIES:
                scan.log(f"  [retry] OpenRouter HTTP {e.code}, retrying in {delay:.1f}s")
                time.sleep(delay)
                delay *= 3
                continue
            raise
        except scan.TRANSIENT_ERRORS as e:
            if attempt < scan.MAX_RETRIES:
                scan.log(f"  [retry] OpenRouter transient error: {e}, retrying in {delay:.1f}s")
                time.sleep(delay)
                delay *= 3
                continue
            raise


def append_analysis(report, analysis):
    DIGEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = __import__("subprocess").check_output(["date", "+%Y-%m-%d %H:%M %Z"]).decode().strip()
    with DIGEST_FILE.open("a", encoding="utf-8") as f:
        f.write(f"\n## {report['company']} ({report['fy']}) — {ts}\n\n{analysis}\n\n[PDF]({report['url']})\n")


def append_raw_list(reports):
    DIGEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    ts = __import__("subprocess").check_output(["date", "+%Y-%m-%d %H:%M %Z"]).decode().strip()
    lines = "\n".join(f"- [{r['company']} ({r['fy']})]({r['url']})" for r in reports)
    with DIGEST_FILE.open("a", encoding="utf-8") as f:
        f.write(f"\n## {ts}\n\n_(no OpenRouter key — raw list only)_\n\n{lines}\n")


def _process_report_task(report, api_key):
    """Worker-thread body: self-paces, does PDF fetch + extraction + OpenRouter call.
    No shared-state mutation here — results are consumed on the main thread."""
    time.sleep(REPORT_FETCH_DELAY_SECONDS)
    text = extract_report_text(report["url"])
    if not text:
        return report, None, "no_text"
    try:
        analysis = analyze_with_openrouter(report, text, api_key)
        return report, analysis, None
    except Exception as e:
        return report, None, e


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Only analyze the first N new reports (for testing)")
    args = parser.parse_args()

    cookie = scan.load_text(scan.COOKIE_FILE)
    api_key = scan.load_openrouter_key()

    if not cookie:
        scan.log(f"No cookie found at {scan.COOKIE_FILE}.")
        sys.exit(1)

    _, body = scan.fetch(ANNUAL_REPORTS_URL, cookie=cookie)
    all_reports = parse_annual_reports(body)
    scan.log(f"Annual reports listed: {len(all_reports)}")

    seen = load_seen()
    new_reports = [r for r in all_reports if r["url"] not in seen]
    if not new_reports:
        scan.log("No new annual reports since last run.")
        return

    if args.limit:
        new_reports = new_reports[: args.limit]
        scan.log(f"Limiting this run to first {len(new_reports)} new reports")

    scan.log(f"New annual reports to analyze: {len(new_reports)}")

    if not api_key:
        scan.log(f"No OpenRouter key found at {scan.KEY_FILE}, logging raw report list only.")
        append_raw_list(new_reports)
        save_seen(seen | {r["url"] for r in new_reports})
        return

    completed = 0
    with ThreadPoolExecutor(max_workers=REPORT_WORKERS) as pool:
        futures = {pool.submit(_process_report_task, r, api_key): r for r in new_reports}
        for future in as_completed(futures):
            report = futures[future]
            _, analysis, err = future.result()
            completed += 1
            if err == "no_text":
                scan.log(f"  [warn] no extractable text for {report['company']}, skipping")
                seen.add(report["url"])
            elif err is not None:
                scan.log(f"  [warn] OpenRouter analysis failed for {report['company']}: {err}, will retry next run")
            else:
                scan.log(f"  Analyzed {report['company']} ({report['fy']})")
                append_analysis(report, analysis)
                seen.add(report["url"])
            if completed % CHECKPOINT_EVERY == 0:
                save_seen(seen)
                scan.log(f"  [checkpoint] {completed}/{len(new_reports)} processed")

    save_seen(seen)
    scan.log("Done.")


if __name__ == "__main__":
    main()
