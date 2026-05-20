#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import time
import unicodedata
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


class GenericPDFDownloader:
    def __init__(self, start_url, base_url=None, pdf_pattern=r"['\"]([^'\"]+?\.pdf)['\"]",
                 out_dir="pdf_downloads", delay_seconds=2.0, save_html_debug=True):
        """
        :param start_url: The page URL to scrape (where PDF links/buttons are listed)
        :param base_url: Base URL for resolving relative PDF paths (defaults to start_url’s domain)
        :param pdf_pattern: Regex to detect PDF URLs in HTML/JS
        :param out_dir: Folder to save PDFs
        """
        self.start_url = start_url
        self.base_url = base_url or start_url
        self.pdf_pattern = pdf_pattern
        self.out_dir = out_dir
        self.delay = delay_seconds
        self.save_html_debug = save_html_debug

        self.sess = requests.Session()
        self.sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/123.0.0.0 Safari/537.36"
        })

        if not os.path.isdir(self.out_dir):
            os.makedirs(self.out_dir, exist_ok=True)

    # ---------- helpers

    @staticmethod
    def _safe_name(s: str) -> str:
        s = unicodedata.normalize("NFKD", s)
        s = re.sub(r"[^\w\s\.-]+", "", s)
        s = re.sub(r"\s+", "_", s.strip())
        return s[:80] if s else "document"

    @staticmethod
    def _looks_like_pdf(bytes_head: bytes) -> bool:
        return bytes_head.startswith(b"%PDF")

    def _normalize_url(self, raw_path: str) -> str:
        path = raw_path.strip()
        if bool(urlparse(path).netloc):  # already full URL
            return path
        return urljoin(self.base_url, path)

    # ---------- scraping

    def fetch_html(self) -> str:
        resp = self.sess.get(self.start_url, timeout=45)
        resp.raise_for_status()
        html = resp.text
        if self.save_html_debug:
            with open(os.path.join(self.out_dir, "debug_source.html"), "w", encoding="utf-8") as f:
                f.write(html)
        return html

    def extract_pdf_links(self, html: str):
        """Extract PDF URLs and human-friendly names."""
        entries = []

        # First try BeautifulSoup for <a href="...pdf">
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            if a["href"].lower().endswith(".pdf"):
                url = self._normalize_url(a["href"])
                name = a.get_text(strip=True) or os.path.basename(urlparse(url).path)
                entries.append({"name": name, "url": url})

        # Regex fallback for JS-based links (onclick, window.open, etc.)
        matches = re.findall(self.pdf_pattern, html, flags=re.I)
        for m in matches:
            url = self._normalize_url(m)
            name = os.path.splitext(os.path.basename(urlparse(url).path))[0]
            entries.append({"name": name, "url": url})

        # Deduplicate
        seen, uniq = set(), []
        for e in entries:
            if e["url"] not in seen:
                uniq.append(e)
                seen.add(e["url"])
        return uniq

    # ---------- downloading

    def download_pdf(self, url: str, name_hint: str) -> bool:
        filename = self._safe_name(name_hint)
        if not filename.lower().endswith(".pdf"):
            filename += ".pdf"
        out_path = os.path.join(self.out_dir, filename)

        if os.path.exists(out_path) and os.path.getsize(out_path) > 1000:
            print(f"Skip (exists): {filename}")
            return True

        headers = {"Referer": self.start_url}
        try:
            with self.sess.get(url, headers=headers, timeout=90, stream=True) as r:
                if r.status_code != 200:
                    print(f"Fail HTTP {r.status_code}: {url}")
                    return False

                head = r.raw.read(5)
                if not self._looks_like_pdf(head):
                    print(f"Not a PDF (content check failed): {url}")
                    return False

                with open(out_path, "wb") as f:
                    f.write(head)
                    for chunk in r.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)

            print(f"OK  ({os.path.getsize(out_path):,} bytes): {filename}")
            return True

        except requests.RequestException as e:
            print(f"Err ({type(e).__name__}): {e}")
            return False

    # ---------- main run

    def run(self, limit=None):
        print("Generic PDF Downloader")
        print("=" * 60)
        print(f"Source page : {self.start_url}")
        print(f"Output dir  : {os.path.abspath(self.out_dir)}")
        print("-" * 60)

        html = self.fetch_html()
        entries = self.extract_pdf_links(html)

        if not entries:
            print("No PDFs found. Check 'debug_source.html' for page content.")
            return

        if limit:
            entries = entries[:int(limit)]

        print(f"Found {len(entries)} PDFs to download.")
        print("-" * 60)

        ok = fail = 0
        for i, e in enumerate(entries, 1):
            print(f"[{i:03d}/{len(entries):03d}] {e['name'][:60]} ... ", end="")
            if self.download_pdf(e["url"], f"{i:03d}_{e['name']}"):
                ok += 1
            else:
                fail += 1
            time.sleep(self.delay)

        print("-" * 60)
        print(f"Done. Success: {ok}, Failed: {fail}")


def main():
    # 👇 EDIT ONLY THESE VARIABLES FOR NEW WEBSITES 👇
    START_URL = "https://www.kviconline.gov.in/pmegp/pmegpweb/docs/jsp/profileView.jsp"
    BASE_URL = "https://www.kviconline.gov.in/pmegp/pmegpweb/docs/jsp/"
    PDF_PATTERN = r"['\"]([^'\"]+?\.pdf)['\"]"   # Regex to catch PDF links in JS/HTML

    downloader = GenericPDFDownloader(
        start_url=START_URL,
        base_url=BASE_URL,
        pdf_pattern=PDF_PATTERN,
        out_dir="pmegp_pdfs",
        delay_seconds=2.0,
        save_html_debug=True
    )
    downloader.run(limit=None)   # set limit=10 for testing


if __name__ == "__main__":
    main()
