#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import time
import unicodedata
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


class PMEGPDownloader:
    LIST_URL = "https://www.kviconline.gov.in/pmegp/pmegpweb/docs/jsp/newprojectReports.jsp"
    # Two useful bases: one pointing to /jsp/ and one to /docs/
    BASE_JSP = "https://www.kviconline.gov.in/pmegp/pmegpweb/docs/jsp/"
    BASE_DOCS = "https://www.kviconline.gov.in/pmegp/pmegpweb/docs/"

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }

    def __init__(self, out_dir="pmegp_pdfs", delay_seconds=1.5, save_html_debug=True):
        self.out_dir = out_dir
        self.delay = float(delay_seconds)
        self.save_html_debug = save_html_debug
        self.sess = requests.Session()
        self.sess.headers.update(self.HEADERS)

        if not os.path.isdir(self.out_dir):
            os.makedirs(self.out_dir, exist_ok=True)

    # ---------- helpers

    @staticmethod
    def _safe_name(s: str) -> str:
        # Normalize unicode, strip, collapse spaces, keep letters/numbers/_-
        s = unicodedata.normalize("NFKD", s)
        s = re.sub(r"[^\w\s\.-]+", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s+", "_", s.strip())
        # keep it sane
        return s[:80] if s else "project"

    @staticmethod
    def _looks_like_pdf(bytes_head: bytes) -> bool:
        return bytes_head.startswith(b"%PDF")

    def _normalize_pdf_url(self, raw_path: str) -> str:
        """
        Normalize the relative/absolute PDF path into a full URL that works.
        Handles:
          - 'commonprojectprofile/...' (singular)
          - 'commonprojectprofiles/...' (plural)
          - paths relative to /docs/jsp/ or /docs/
        """
        path = raw_path.strip().replace("\\", "/")

        # If it’s already absolute, return as-is
        if bool(urlparse(path).netloc):
            return path

        # If it starts with /pmegp/… use site root directly
        if path.startswith("/"):
            return urljoin("https://www.kviconline.gov.in", path)

        # A lot of links are like 'commonprojectprofile/XYZ.pdf' or 'commonprojectprofiles/XYZ.pdf'
        # Those should be resolved against /docs/ not /docs/jsp/
        if path.lower().startswith(("commonprojectprofile/", "commonprojectprofiles/")):
            return urljoin(self.BASE_DOCS, path)

        # Fallback: resolve relative to the JSP directory
        return urljoin(self.BASE_JSP, path)

    # ---------- scraping

    def fetch_list_html(self) -> str:
        resp = self.sess.get(self.LIST_URL, timeout=45)
        resp.raise_for_status()
        html = resp.text
        if self.save_html_debug:
            with open(os.path.join(self.out_dir, "debug_pmegp_list.html"), "w", encoding="utf-8") as f:
                f.write(html)
        return html

    def extract_pdf_entries(self, html: str):
        """
        Return list of dicts: [{'name': 'Bakery Products', 'url': 'https://...pdf'}, ...]
        Strategy:
          1) Parse rows to get nicer names when possible.
          2) Regex-scan whole HTML for any .pdf references as a hard fallback.
        """
        entries = []

        # 1) Try to pair names with their 'View' js calls within the same row
        soup = BeautifulSoup(html, "html.parser")
        for row in soup.find_all("tr"):
            tds = row.find_all(["td", "th"])
            if len(tds) < 2:
                continue

            # Candidate name lives in 2nd cell typically
            candidate_name = tds[1].get_text(" ", strip=True)
            onclick_el = None

            # look for input/button/a with onclick or href
            onclick_el = (
                row.find("input", attrs={"type": re.compile(r"button|submit", re.I)}) or
                row.find("a", href=True) or
                row.find("button")
            )

            pdf_url = None
            if onclick_el:
                # onclick patterns
                oc = onclick_el.get("onclick", "") or ""
                # openFile('...pdf') or window.open('...pdf')
                m = re.search(r"(?:openFile|window\.open)\s*\(\s*['\"]([^'\"]+\.pdf)['\"]", oc, re.I)
                if m:
                    pdf_url = self._normalize_pdf_url(m.group(1))
                # direct anchor link
                if not pdf_url and onclick_el.name == "a":
                    href = onclick_el.get("href", "")
                    if href and href.lower().endswith(".pdf"):
                        pdf_url = self._normalize_pdf_url(href)

            if pdf_url:
                entries.append({
                    "name": candidate_name or os.path.basename(urlparse(pdf_url).path),
                    "url": pdf_url
                })

        # 2) Fallback: raw regex over entire HTML for any PDF path
        if not entries:
            raw_hits = re.findall(
                r"(?:openFile|window\.open)\s*\(\s*['\"]([^'\"]+?\.pdf)['\"]",
                html,
                flags=re.I,
            )
            # also catch plain href="...pdf"
            raw_hits += re.findall(
                r"href\s*=\s*['\"]([^'\"]+?\.pdf)['\"]",
                html,
                flags=re.I,
            )

            # dedupe while keeping order
            seen = set()
            for p in raw_hits:
                full = self._normalize_pdf_url(p)
                if full in seen:
                    continue
                seen.add(full)
                # Name = filename without extension (prettified)
                stem = os.path.splitext(os.path.basename(urlparse(full).path))[0]
                name = stem.replace("_", " ").replace("-", " ").title()
                entries.append({"name": name, "url": full})

        # final dedupe by url (sometimes appears twice)
        uniq, seen = [], set()
        for e in entries:
            if e["url"] in seen:
                continue
            seen.add(e["url"])
            uniq.append(e)

        return uniq

    # ---------- downloading

    def download_pdf(self, url: str, name_hint: str) -> bool:
        # safe name from hint or filename
        filename = self._safe_name(name_hint)
        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"
        out_path = os.path.join(self.out_dir, filename)

        if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
            print(f"Skip (exists): {filename}")
            return True

        # Many servers check Referer for deep asset pulls
        headers = {"Referer": self.LIST_URL}
        try:
            with self.sess.get(url, headers=headers, timeout=90, stream=True) as r:
                if r.status_code != 200:
                    print(f"Fail HTTP {r.status_code}: {url}")
                    return False

                # Peek first bytes to verify it's a PDF
                head = r.raw.read(5)
                if not self._looks_like_pdf(head):
                    print(f"Not a PDF (content check failed): {url}")
                    return False

                # Write file (include head we already read)
                with open(out_path, "wb") as f:
                    f.write(head)
                    for chunk in r.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)

            size = os.path.getsize(out_path)
            print(f"OK  ({size:,} bytes): {filename}")
            return True

        except requests.RequestException as e:
            print(f"Err ({type(e).__name__}): {e}")
            return False

    # ---------- public API

    def run(self, limit=None):
        print("PMEGP PDF Downloader")
        print("=" * 60)
        print(f"Source page : {self.LIST_URL}")
        print(f"Output dir  : {os.path.abspath(self.out_dir)}")
        print("-" * 60)

        html = self.fetch_list_html()
        entries = self.extract_pdf_entries(html)

        if not entries:
            print("No PDF entries found. Check 'debug_pmegp_list.html' saved in the output folder.")
            return

        if limit:
            entries = entries[:int(limit)]

        print(f"Found {len(entries)} PDF links to download.")
        print("-" * 60)

        ok = fail = 0
        for i, e in enumerate(entries, 1):
            print(f"[{i:03d}/{len(entries):03d}] {e['name'][:70]} ... ", end="")
            success = self.download_pdf(e["url"], f"{i:03d}_{e['name']}")
            if success:
                ok += 1
            else:
                fail += 1
            time.sleep(self.delay)

        print("-" * 60)
        print(f"Done. Success: {ok}, Failed: {fail}")


def main():
    # Tweak delay if the site seems slow; keep it polite.
    dl = PMEGPDownloader(out_dir="pmegp_pdfs", delay_seconds=2.0, save_html_debug=True)
    dl.run(limit=None)  # set e.g. limit=20 for a quick test


if __name__ == "__main__":
    main()
