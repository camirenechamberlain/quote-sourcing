from __future__ import annotations

import re
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (compatible; QuoteSourcingBot/1.0; +local RFQ tool, contact via site owner)"
FETCH_TIMEOUT_S = 8
MAX_RESPONSE_BYTES = 2_000_000
MAX_PAGES_PER_SITE = 3
CONTACT_LINK_PATTERN = re.compile(r"contact|about|get-in-touch", re.IGNORECASE)
EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
JUNK_DOMAINS = {"example.com", "sentry.io", "wixpress.com", "godaddy.com", "domain.com"}
JUNK_PREFIXES = {"noreply", "no-reply", "postmaster", "webmaster", "abuse"}


@dataclass
class ScrapeResult:
    email: str | None
    source: str  # scraped_mailto | scraped_regex | none
    status: str  # success | no_email_found | fetch_failed | robots_disallowed
    notes: str | None = None


def _is_junk_email(email: str) -> bool:
    local, _, domain = email.partition("@")
    domain = domain.lower()
    if domain in JUNK_DOMAINS:
        return True
    if local.lower() in JUNK_PREFIXES:
        return True
    if re.search(r"\.(png|jpg|jpeg|gif|svg|webp)$", email, re.IGNORECASE):
        return True
    return False


def _robots_allows(base_url: str) -> bool:
    parsed = urlparse(base_url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    try:
        resp = requests.get(robots_url, timeout=5, headers={"User-Agent": USER_AGENT})
        if resp.status_code >= 400:
            return True  # no robots.txt (or inaccessible) -> treat as allowed
        rp.parse(resp.text.splitlines())
        return rp.can_fetch(USER_AGENT, base_url)
    except requests.RequestException:
        return True  # can't check -> don't block on a transient robots.txt failure


def _fetch(url: str) -> str | None:
    resp = requests.get(
        url,
        timeout=FETCH_TIMEOUT_S,
        headers={"User-Agent": USER_AGENT},
        stream=True,
    )
    resp.raise_for_status()
    content = resp.raw.read(MAX_RESPONSE_BYTES, decode_content=True)
    return content.decode(resp.encoding or "utf-8", errors="ignore")


def _extract_emails_from_html(html: str, base_url: str) -> tuple[list[str], list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    domain = urlparse(base_url).netloc.replace("www.", "")

    mailto_emails = []
    for a in soup.find_all("a", href=True):
        if a["href"].lower().startswith("mailto:"):
            addr = a["href"][7:].split("?")[0].strip()
            if addr and not _is_junk_email(addr):
                mailto_emails.append(addr)

    text_emails = [e for e in EMAIL_PATTERN.findall(soup.get_text(" ")) if not _is_junk_email(e)]

    def same_domain_first(emails: list[str]) -> list[str]:
        return sorted(set(emails), key=lambda e: 0 if domain in e.lower() else 1)

    return same_domain_first(mailto_emails), same_domain_first(text_emails)


def _find_contact_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if CONTACT_LINK_PATTERN.search(href) or CONTACT_LINK_PATTERN.search(a.get_text(" ")):
            links.append(urljoin(base_url, href))
    # de-dupe, keep order
    seen = set()
    ordered = []
    for link in links:
        if link not in seen and urlparse(link).netloc == urlparse(base_url).netloc:
            seen.add(link)
            ordered.append(link)
    return ordered


def scrape_company_email(website: str | None) -> ScrapeResult:
    if not website:
        return ScrapeResult(email=None, source="none", status="no_email_found", notes="No website on record")

    url = website if website.startswith("http") else f"https://{website}"

    if not _robots_allows(url):
        return ScrapeResult(email=None, source="none", status="robots_disallowed", notes="robots.txt disallows fetching")

    pages_fetched = 0
    pages_to_try = [url]
    tried = set()

    while pages_to_try and pages_fetched < MAX_PAGES_PER_SITE:
        page_url = pages_to_try.pop(0)
        if page_url in tried:
            continue
        tried.add(page_url)
        try:
            html = _fetch(page_url)
            pages_fetched += 1
        except requests.RequestException as e:
            if pages_fetched == 0:
                return ScrapeResult(email=None, source="none", status="fetch_failed", notes=str(e)[:200])
            continue

        if html is None:
            continue

        mailto_emails, text_emails = _extract_emails_from_html(html, url)
        if mailto_emails:
            return ScrapeResult(email=mailto_emails[0], source="scraped_mailto", status="success")
        if text_emails:
            return ScrapeResult(email=text_emails[0], source="scraped_regex", status="success")

        if pages_fetched == 1:
            for link in _find_contact_links(html, url)[:2]:
                if link not in tried:
                    pages_to_try.append(link)

    return ScrapeResult(email=None, source="none", status="no_email_found", notes=f"Checked {pages_fetched} page(s), no email found")
