#!/usr/bin/env -S uv run
# /// script
# dependencies = [
#   "beautifulsoup4",
# ]
# ///

"""Extract [paper]-tagged emails from Thunderbird mbox folders to CSV.

Scans both INBOX and Sent Items folders for [paper]-tagged messages and
combines them into a single deduplicated CSV.

Usage:
    uv run extract_papers.py

Environment:
    MAILBOX:     path to Thunderbird INBOX mbox file
                (default: ~/.thunderbird/.../ImapMail/outlook.office365.com/INBOX)
    SENT_MAILBOX: path to Thunderbird Sent Items mbox file
                (default: ~/.thunderbird/.../ImapMail/outlook.office365.com/Sent Items-1)

Output:
    papers.csv in the same directory as this script.
"""

import csv
import logging
import mmap
import os
import re
import subprocess
import sys
import html as html_mod
import quopri
import base64
from bisect import bisect_right
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("extract_papers")

SCRIPT_DIR = Path(__file__).parent.resolve()
OUTPUT = SCRIPT_DIR / "papers.csv"

THUNDERBIRD = (
    Path.home()
    / ".thunderbird"
    / "7z5edwgd.default-release"
    / "ImapMail"
    / "outlook.office365.com"
)

# Each entry: (label, mbox_path, offsets_path, lines_path)
MBOX_CONFIGS: list[tuple[str, Path, Path, Path]] = []

_inbox = Path(os.environ.get("MAILBOX", THUNDERBIRD / "INBOX"))
MBOX_CONFIGS.append(("inbox", _inbox,
    SCRIPT_DIR / "from_offsets_inbox.txt",
    SCRIPT_DIR / "paper_lines_inbox.txt"))

_sent = os.environ.get("SENT_MAILBOX", str(THUNDERBIRD / "Sent Items-1"))
_sent_path = Path(_sent)
if _sent_path.exists():
    MBOX_CONFIGS.append(("sent", _sent_path,
        SCRIPT_DIR / "from_offsets_sent.txt",
        SCRIPT_DIR / "paper_lines_sent.txt"))
else:
    log.info("Sent mailbox not found at %s, skipping", _sent_path)


# ── Index building (fast grep on the 12 GB mbox) ──────────────────────


def build_indexes(mbox_path: Path, offsets_path: Path, lines_path: Path):
    """Run grep to build message-boundary and paper-subject indexes for one mbox."""
    log.info("Building message boundary index for %s ...", mbox_path.name)
    subprocess.run(
        ["grep", "-nb", "^From ", str(mbox_path)],
        stdout=open(offsets_path, "w"),
        stderr=subprocess.DEVNULL,
        check=True,
    )
    log.info("Built %s (%d lines)", offsets_path.name, offsets_path.stat().st_size)

    log.info("Building paper subject index for %s ...", mbox_path.name)
    subprocess.run(
        ["grep", "-n", "^Subject: \\[paper\\]", str(mbox_path)],
        stdout=open(lines_path, "w"),
        stderr=subprocess.DEVNULL,
        check=True,
    )
    log.info("Built %s (%d lines)", lines_path.name, lines_path.stat().st_size)


def parse_from_offsets(path: Path) -> list[tuple[int, int]]:
    """Return sorted list of (line_number, byte_offset) for mbox message starts."""
    entries = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(":", 2)
            if len(parts) >= 2:
                try:
                    entries.append((int(parts[0]), int(parts[1])))
                except ValueError:
                    continue
    return entries


def parse_paper_lines(path: Path) -> list[int]:
    """Return sorted list of Subject: [paper] line numbers."""
    lines = []
    with path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(":", 1)
            if parts:
                try:
                    lines.append(int(parts[0]))
                except ValueError:
                    continue
    return lines


# ── Message extraction from raw mbox ──────────────────────────────────


def extract_header(headers_text: str, name: str) -> str:
    """Extract a (potentially multi-line) header value by name."""
    val = ""
    in_header = False
    prefix = name + ":"
    for line in headers_text.split("\n"):
        line_stripped = line.rstrip("\r")
        if line_stripped.upper().startswith(prefix.upper()):
            val = line_stripped[len(prefix):].strip()
            in_header = True
        elif in_header and line_stripped and line_stripped[0] in (" ", "\t"):
            val += " " + line_stripped.strip()
        elif in_header:
            break
    return val


def extract_message_range(
    mbox_path: Path,
    start_byte: int,
    end_byte: int | None,
) -> tuple[str, str | None]:
    """Extract headers text and raw body from a byte range in the mbox."""
    with open(mbox_path, "rb") as f:
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            size = len(mm)
            if end_byte is None or end_byte > size:
                end_byte = size
            chunk = mm[start_byte:end_byte]

    text = chunk.decode("utf-8", errors="replace")

    # Split headers / body at first blank line
    parts = re.split(r"\r?\n\r?\n", text, maxsplit=1)
    if len(parts) < 2:
        return "", None

    header_section = parts[0]
    body_section = parts[1]

    # Remove leading mbox "From " separator
    header_lines = header_section.split("\n", 1)
    if header_lines[0].startswith("From "):
        header_section = header_lines[1] if len(header_lines) > 1 else ""

    return header_section, body_section


def _decode_qp(raw: str) -> str:
    try:
        decoded = quopri.decodestring(raw.encode("ascii", errors="replace"))
        return decoded.decode("utf-8", errors="replace")
    except Exception:
        return raw


def _decode_body_with_charset(raw: str, charset: str) -> str:
    for enc in [charset, "utf-8", "latin-1", "iso-8859-1", "cp1252"]:
        if not enc:
            continue
        try:
            return raw.encode("latin-1").decode(enc, errors="replace")
        except Exception:
            try:
                return raw.encode("ascii").decode(enc, errors="replace")
            except Exception:
                continue
    return raw


def maybe_decode_transfer_encoding(part_text: str, headers: str) -> str:
    """Apply quoted-printable or base64 decoding if the headers indicate it."""
    if "Content-Transfer-Encoding: base64" in headers:
        # Remove any whitespace from base64 text
        b64 = re.sub(r"\s", "", part_text.strip())
        try:
            decoded = base64.b64decode(b64)
            # Try to guess charset
            cs = "utf-8"
            m = re.search(r'charset="?([^"\s;]+)', headers, re.IGNORECASE)
            if m:
                cs = m.group(1)
            return decoded.decode(cs, errors="replace")
        except Exception:
            return part_text

    if "Content-Transfer-Encoding: quoted-printable" in headers:
        return _decode_qp(part_text)

    return part_text


def extract_body_text(
    body_section: str,
    content_type: str = "",
    content_transfer_encoding: str = "",
) -> str | None:
    """Extract the text/html or text/plain part from the body section.

    content_type / content_transfer_encoding come from the *main* headers
    (used for non-multipart messages).
    """
    # Get boundary from Content-Type header (not from body)
    boundary = None
    m = re.search(r'boundary="([^"]+)"', content_type, re.IGNORECASE)
    if m:
        boundary = m.group(1)
    else:
        # Fallback: search in body (some clients repeat it)
        m = re.search(r'boundary="([^"]+)"', body_section[:3000], re.IGNORECASE)
        if m:
            boundary = m.group(1)

    if boundary:
        parts = body_section.split("--" + boundary)
        for part in parts:
            header_end = re.search(r"\r?\n\r?\n", part)
            if header_end is None:
                continue
            sub_headers = part[: header_end.start()]
            sub_body = part[header_end.end():].strip()
            sub_body = sub_body.rstrip("-").strip()

            ct = extract_header(sub_headers, "Content-Type")
            cte = extract_header(sub_headers, "Content-Transfer-Encoding")
            if "text/html" in ct:
                return _decode_sub_body(sub_body, cte)
            if "text/plain" in ct and "text/html" not in (
                extract_header(body_section[:3000], "Content-Type")
            ):
                return _decode_sub_body(sub_body, cte)
        return None

    # Not multipart – use main-header CTE
    decoded = body_section
    if content_transfer_encoding:
        decoded = _decode_sub_body(decoded, content_transfer_encoding)

    # Strip trailing mbox "From " separator if splice was imprecise
    decoded = re.sub(r"\nFrom[ -].*", "\n", decoded, count=1).strip()
    return decoded


def _decode_sub_body(text: str, encoding: str) -> str:
    """Decode a sub-part according to its Content-Transfer-Encoding."""
    if "base64" in encoding.lower():
        b64 = re.sub(r"\s", "", text.strip())
        try:
            raw = base64.b64decode(b64)
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return text
    if "quoted-printable" in encoding.lower():
        return _decode_qp(text)
    return text


# ── URL extraction ────────────────────────────────────────────────────


def extract_originalsrc(body: str) -> str | None:
    for m in re.finditer(r'originalsrc="([^"]+)"', body):
        url = html_mod.unescape(m.group(1))
        url = re.sub(r"^[^a-zA-Z]*", "", url)
        if not url.startswith(("http://", "https://")):
            continue
        # Skip URLs that look like generic homepages (no meaningful path)
        path = re.sub(r"https?://[^/]+", "", url).strip("/")
        if not path or path.count("/") == 0:
            continue
        return url
    return None


def extract_safelink_url(body: str) -> str | None:
    m = re.search(
        r"https?://eur\d*\.safelinks\.protection\.outlook\.com/\?url=([^&\"\s]+)",
        body,
    )
    return unquote(m.group(1)) if m else None


def extract_direct_href(body: str) -> str | None:
    m = re.search(r'<a\s[^>]*href="([^"]+)"', body)
    if m:
        url = m.group(1)
        if url.startswith(("http://", "https://")):
            return url
    return None


def extract_plain_url(body: str) -> str | None:
    """Fallback: find any http(s) URL in plain text."""
    for m in re.finditer(r"https?://[^\s<>\"']{10,}", body):
        url = m.group(0)
        if "safelinks.protection.outlook.com" in url:
            continue
        url = html_mod.unescape(url)
        # Strip leading non-URL junk from QP artifacts (e.g. "=https://" -> "https://")
        url = re.sub(r"^[^a-zA-Z]*", "", url)
        if url.startswith(("http://", "https://")):
            return url
    return None


def extract_direct_href(body: str) -> str | None:
    """Extract the first http(s) href from an <a> tag."""
    for m in re.finditer(r'<a\s[^>]*href="([^"]+)"', body, re.IGNORECASE):
        url = html_mod.unescape(m.group(1))
        # Strip leading non-URL junk from QP artifacts
        url = re.sub(r"^[^a-zA-Z]*", "", url)
        if url.startswith(("http://", "https://")):
            return url
    return None


def extract_url(body: str) -> str:
    """Best-effort extraction: originalsrc > plain > href > safelink."""
    for fn in (
        extract_originalsrc,
        extract_plain_url,
        extract_direct_href,
        extract_safelink_url,
    ):
        url = fn(body)
        if url:
            return url
    return ""


# ── Title and comment extraction ──────────────────────────────────────


_BOILERPLATE = {
    "dear", "best", "thanks", "regards", "sent from",
    "get outlook for", "microsoft", "original message",
    "from:", "to:", "cc:", "subject:", "date:",
    "forwarded message", "automatic reply", "out of office",
}


def _is_boilerplate(text: str) -> bool:
    t = text.lower().strip()
    if not t or len(t) < 3:
        return True
    if t in ("br", "nbsp", "&nbsp;", "=20", "=3d", "reply"):
        return True
    if any(t.startswith(b) for b in _BOILERPLATE):
        return True
    if sum(c.isalpha() for c in t) < 3:
        return True
    return False


def _strip_style_script(html_text: str) -> str:
    """Remove <style>...</style> and <script>...</script> blocks."""
    html_text = re.sub(
        r'<style[^>]*>.*?</style>', "", html_text, flags=re.DOTALL | re.IGNORECASE
    )
    html_text = re.sub(
        r'<script[^>]*>.*?</script>', "", html_text, flags=re.DOTALL | re.IGNORECASE
    )
    return html_text


_COMMENT_PREFIXES = (
    "here is", "here's", "here:", "sorry", "interesting", "re:",
    "also", "check", "also check", "just", "plus", "btw",
)


def _is_comment_subject(subject: str) -> bool:
    """Detect if the Subject line is a personal comment, not the paper title."""
    s = subject.strip().lower()
    if s.startswith(("^", "–", "-", "•")):
        return True
    if s.startswith(_COMMENT_PREFIXES):
        return True
    # "paper" as first word (not part of title)
    if re.match(r"^(paper|here)\b", s):
        return True
    return False


def _looks_like_title(text: str) -> bool:
    """Heuristic: does this text look like a paper title?"""
    if len(text) < 20 or len(text) > 300:
        return False
    # Must start with uppercase letter
    if not text[0].isupper():
        return False
    t = text.lower()
    # Reject comment-like starts
    if t.startswith(_COMMENT_PREFIXES):
        return False
    if t.startswith(("dear", "best", "thanks", "regards", "sent", "get")):
        return False
    # Must have at least 4 words
    if len(t.split()) < 4:
        return False
    # Should not contain email addresses
    if re.search(r"[\w.+-]+@[\w-]+\.", t):
        return False
    # Should not end with obvious file extension
    if re.search(r"\.(pdf|docx?|pptx?|xlsx?)$", t):
        return False
    # Reject lines with too many non-alpha chars
    alpha = sum(c.isalpha() for c in t)
    if alpha < len(t) * 0.4:
        return False
    return True


def _find_title_in_body(body: str) -> str | None:
    """Find the best paper-title candidate in the body text."""
    if BeautifulSoup:
        try:
            soup = BeautifulSoup(body, "html.parser")
            for tag in soup.find_all(["div", "span", "p"]):
                text = tag.get_text(strip=True)
                if not text:
                    continue
                if "http" in text or "www." in text or "mailto:" in text:
                    continue
                if tag.get("id", "").startswith(("ms-outlook", "Signature")):
                    continue
                text = re.sub(r"\s+", " ", text).strip()
                text = html_mod.unescape(text)
                if _looks_like_title(text):
                    return text
        except Exception:
            pass

    # Fallback: regex text extraction from plain text
    clean = re.sub(r"<[^>]+>", "\n", body)
    for line in clean.split("\n"):
        line = html_mod.unescape(line.strip())
        if not line or "http" in line or "mailto:" in line:
            continue
        if _is_boilerplate(line):
            continue
        line = re.sub(r"\s+", " ", line).strip()
        if _looks_like_title(line):
            return line

    return None


def _has_continuation(text: str) -> bool:
    """Check if text has QP/header continuation markers."""
    return text.endswith("=") or "= " in text


def extract_title_and_comment(
    body: str, subject_title: str, url: str
) -> tuple[str, str]:
    """Extract (title, comment) from body.

    When the Subject line is itself a comment (e.g. "Here is a paper: ..."),
    the title is taken from the body and the subject becomes the comment.
    Otherwise the title stays as the subject (possibly expanded from body).
    """
    if not body:
        return subject_title, ""

    body = _strip_style_script(body)
    body = _strip_signature(body)
    subject_norm = re.sub(r"\s+", " ", subject_title).strip().lower()
    body_title = _find_title_in_body(body)

    # Case 1: Subject is a comment → swap with body title
    if _is_comment_subject(subject_title):
        if body_title:
            return body_title, subject_title
        # No good body title found; use subject as title but also as comment
        return subject_title, subject_title

    # Case 2: Subject is the title, but may be truncated (QP continuation)
    if _has_continuation(subject_title) and body_title:
        # Check if body title starts with the subject (expansion)
        if body_title.lower().startswith(subject_norm.rstrip("=").rstrip()):
            return body_title, _extract_comment_from_body(body, body_title, url)
        # Also check: if continuing into body title after stripping QP junk
        clean_subj = re.sub(r"\s*=\s*", " ", subject_title).strip()
        if body_title.lower().startswith(clean_subj.lower()[:40]):
            return body_title, _extract_comment_from_body(body, body_title, url)

    # Case 3: Subject is the title, keep it
    comment = _extract_comment_from_body(body, subject_title, url)
    return subject_title, comment


def _strip_signature(body: str) -> str:
    """Remove email signature elements from HTML body text."""
    if not BeautifulSoup:
        return body
    try:
        soup = BeautifulSoup(body, "html.parser")
        # Remove elements with signature-related IDs or classes
        for selector in (
            "[id*=signature i]", "[id*=Signature]",
            "[class*=signature i]", "[class*=Signature]",
            "[id*=ms-outlook i]",
        ):
            for elem in soup.select(selector):
                elem.decompose()
        return str(soup)
    except Exception:
        return body


def _extract_comment_from_body(body: str, title: str, url: str) -> str:
    """Return body text that is not the title, URL, or boilerplate."""
    if not body:
        return ""

    body = _strip_style_script(body)

    if BeautifulSoup:
        try:
            soup = BeautifulSoup(body, "html.parser")
            text = soup.get_text(separator="\n")
        except Exception:
            text = re.sub(r"<[^>]+>", "\n", body)
    else:
        text = re.sub(r"<[^>]+>", "\n", body)

    text = html_mod.unescape(text)

    url_norm = url.rstrip("/")
    title_norm = re.sub(r"\s+", " ", title).strip()

    lines = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if _is_boilerplate(line):
            continue
        if re.sub(r"\s+", " ", line).strip() == title_norm:
            continue
        if line.rstrip("/") == url_norm or line == url:
            continue
        if "http" in line:
            continue
        if len(line) < 3:
            continue
        lines.append(line)

    comment = " ".join(lines)
    return re.sub(r"\s+", " ", comment).strip()


# ── Date parsing ──────────────────────────────────────────────────────


_DATE_FORMATS = [
    "%a, %d %b %Y %H:%M:%S %z",
    "%a, %d %b %Y %H:%M:%S %Z",
    "%d %b %Y %H:%M:%S %z",
    "%d %b %Y %H:%M:%S %Z",
    "%a, %d %b %Y %H:%M:%S %z (%Z)",
]


def parse_date(date_str: str) -> str:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(date_str.strip(), fmt).isoformat()
        except ValueError:
            continue
    return date_str


# ── Main pipeline ─────────────────────────────────────────────────────


def process_message(
    header_section: str, body_section: str | None
) -> dict | None:
    """Parse a single paper email into a dict."""
    subj = extract_header(header_section, "Subject")
    if not subj.startswith("[paper]") or subj.upper().startswith("RE:"):
        return None

    title = subj[len("[paper]"):].strip()
    if not title:
        return None

    content_type = extract_header(header_section, "Content-Type")
    content_transfer_encoding = extract_header(
        header_section, "Content-Transfer-Encoding"
    )
    body = (
        extract_body_text(body_section, content_type, content_transfer_encoding)
        if body_section
        else None
    )

    url = extract_url(body) if body else ""
    title, comment = extract_title_and_comment(body, title, url) if body else (title, "")

    from_addr = extract_header(header_section, "From")
    m = re.search(r"<([^>]+)>", from_addr)
    if m:
        from_addr = m.group(1)

    date_str = extract_header(header_section, "Date")
    date_parsed = parse_date(date_str) if date_str else ""

    return {
        "title": title,
        "url": url,
        "from": from_addr or "",
        "date": date_str or "",
        "date_parsed": date_parsed,
        "comment": comment,
        "keywords": extract_header(header_section, "Keywords") or "",
    }


def extract_papers(
    mbox_path: Path, offsets_path: Path, lines_path: Path
) -> list[dict]:
    """Build/load indexes and extract [paper] messages from one mbox."""
    if not offsets_path.exists() or not lines_path.exists():
        build_indexes(mbox_path, offsets_path, lines_path)
    else:
        log.info("Using existing indexes for %s", mbox_path.name)

    boundaries = parse_from_offsets(offsets_path)
    log.info("%s: %d message boundaries", mbox_path.name, len(boundaries))

    paper_lines = parse_paper_lines(lines_path)
    log.info("%s: %d paper subject lines", mbox_path.name, len(paper_lines))

    boundary_lines = [b[0] for b in boundaries]
    boundary_bytes = [b[1] for b in boundaries]

    papers = []
    skipped = 0

    for i, paper_line in enumerate(paper_lines, 1):
        if i % 100 == 0:
            log.info("%s: progress %d/%d", mbox_path.name, i, len(paper_lines))

        idx = bisect_right(boundary_lines, paper_line) - 1
        if idx < 0:
            skipped += 1
            continue

        start_byte = boundary_bytes[idx]
        end_byte = (
            boundary_bytes[idx + 1] if idx + 1 < len(boundary_bytes) else None
        )

        header_section, body_section = extract_message_range(
            mbox_path, start_byte, end_byte
        )
        if not header_section:
            skipped += 1
            continue

        msg = process_message(header_section, body_section)
        if msg:
            papers.append(msg)
        else:
            skipped += 1

    log.info("%s: extracted %d papers, skipped %d", mbox_path.name, len(papers), skipped)
    return papers


def main():
    all_papers: list[dict] = []

    for label, mbox_path, offsets_path, lines_path in MBOX_CONFIGS:
        log.info("mbox: %s (%s)", mbox_path, mbox_path.stat().st_size)
        extracted = extract_papers(mbox_path, offsets_path, lines_path)
        all_papers.extend(extracted)

    # Deduplicate by title (case-insensitive, keep first occurrence)
    seen = set()
    deduped = []
    for p in all_papers:
        key = p["title"].strip().lower()
        if key and key not in seen:
            seen.add(key)
            deduped.append(p)

    n_dup = len(all_papers) - len(deduped)
    log.info("Total extracted: %d, duplicates removed: %d, final: %d",
             len(all_papers), n_dup, len(deduped))

    with open(OUTPUT, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "title", "url", "from", "date", "date_parsed",
                "comment", "keywords",
            ],
        )
        writer.writeheader()
        writer.writerows(deduped)

    log.info("Wrote %s (%d rows)", OUTPUT, len(deduped))


if __name__ == "__main__":
    main()
