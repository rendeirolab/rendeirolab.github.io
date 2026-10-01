#!/usr/bin/env -S uv run
# /// script
# dependencies = [
#   "beautifulsoup4",
#   "pyyaml",
# ]
# ///

"""Extract [paper]-tagged emails to CSV from moe (primary) or Thunderbird mbox (fallback).

Reads [paper]-tagged messages from the local moe SQLite store
(~/.local/share/moe/moe.db) by default, scanning both the INBOX and Sent
Items folders and combining them into a single deduplicated CSV. When no
moe database is configured (or the file is missing), it falls back to the
legacy Thunderbird mbox scanner.

The run is refused (exit code 1) when the moe store has not synced in the
last 7 days (or, in mbox mode, when no [paper]-tagged email was found in
the last 7 days), which usually means the mailbox was not synced. Override
with ALLOW_STALE_MAIL=1.

Usage:
    uv run extract_papers.py

Configuration (no built-in default):
    1. Environment variables: MOE_DB, MOE_INBOX, MOE_SENT_FOLDERS
       (comma-separated), or MAILBOX / SENT_MAILBOX for the mbox fallback.
    2. `cool_papers:` section in the project's config.yaml
       (keys: moe_db, inbox_folder, sent_folders, mailbox, sent_mailbox).
    The moe INBOX folder is mandatory; sent folders are optional.
    When the moe source is unavailable, the mbox INBOX is mandatory.

Output:
    papers.csv in the same directory as this script.
"""

import csv
import logging
import mmap
import os
import re
import sqlite3
import subprocess
import sys
import warnings
import html as html_mod
import quopri
import base64
from bisect import bisect_right
from datetime import datetime, timezone
from email.header import decode_header
from pathlib import Path
from urllib.parse import unquote, urlparse

try:
    from bs4 import BeautifulSoup
    from bs4 import MarkupResemblesLocatorWarning
    warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)
except ImportError:
    BeautifulSoup = None

try:
    import yaml
except ImportError:
    yaml = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("extract_papers")

SCRIPT_DIR = Path(__file__).parent.resolve()
OUTPUT = SCRIPT_DIR / "papers.csv"

# Sanity cap on a single message byte range; a corrupt index must never
# cause a huge slice+decode (which previously ballooned memory to > 15 GB).
MAX_MESSAGE_BYTES = 100 * 1024 * 1024


def load_config() -> dict:
    """Load the cool_papers section from the project's config.yaml."""
    cfg_path = SCRIPT_DIR.parent / "config.yaml"
    if not cfg_path.exists() or yaml is None:
        return {}
    try:
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
        return cfg.get("cool_papers", {}) or {}
    except Exception:
        return {}


def resolve_mailboxes() -> list[tuple[str, Path, Path, Path]]:
    """Resolve mbox sources from env vars or config.yaml.

    The INBOX mailbox is mandatory (no built-in default, so a stale or
    missing configuration fails loudly instead of silently using a wrong
    path). The sent mailbox is optional.

    Returns a list of (label, mbox_path, offsets_path, lines_path).
    """
    cfg = load_config()
    inbox_raw = os.environ.get("MAILBOX") or cfg.get("mailbox")
    if not inbox_raw:
        log.error(
            "No INBOX mailbox configured: set 'cool_papers.mailbox' in "
            "config.yaml or the MAILBOX environment variable."
        )
        sys.exit(1)
    mboxes = [
        (
            "inbox",
            Path(inbox_raw).expanduser(),
            SCRIPT_DIR / "from_offsets_inbox.txt",
            SCRIPT_DIR / "paper_lines_inbox.txt",
        )
    ]

    sent_raw = os.environ.get("SENT_MAILBOX") or cfg.get("sent_mailbox")
    sent_path = Path(sent_raw).expanduser() if sent_raw else None
    if sent_path is not None:
        if sent_path.exists():
            mboxes.append(
                (
                    "sent",
                    sent_path,
                    SCRIPT_DIR / "from_offsets_sent.txt",
                    SCRIPT_DIR / "paper_lines_sent.txt",
                )
            )
        else:
            log.info("Sent mailbox not found at %s, skipping", sent_path)
    else:
        log.info("No sent mailbox configured, skipping")

    return mboxes


# ── moe source (primary) ──────────────────────────────────────────────


def resolve_moe() -> tuple[Path, list[str], list[str]] | None:
    """Resolve the moe SQLite DB and folder names from env vars or config.yaml.

    Returns (db_path, inbox_folders, sent_folders), or None when no moe
    database is configured or the file does not exist (caller falls back
    to the mbox source).
    """
    cfg = load_config()
    db_raw = os.environ.get("MOE_DB") or cfg.get("moe_db")
    if not db_raw:
        return None
    db_path = Path(db_raw).expanduser()
    if not db_path.exists():
        log.info("moe database not found at %s; falling back to mbox", db_path)
        return None

    inbox_raw = os.environ.get("MOE_INBOX") or cfg.get("inbox_folder")
    inbox_folders = _split_folders(inbox_raw) or ["Inbox"]

    sent_raw = os.environ.get("MOE_SENT_FOLDERS") or cfg.get("sent_folders")
    sent_folders = _split_folders(sent_raw) or ["Sent Items"]

    return db_path, inbox_folders, sent_folders


def _split_folders(raw) -> list[str]:
    """Normalize a folder-name config value into a list of names."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [s.strip() for s in raw.split(",") if s.strip()]
    return [str(s).strip() for s in raw if str(s).strip()]


def moe_folder_ids(conn: sqlite3.Connection, names: list[str]) -> dict[str, str]:
    """Map folder names to folder ids (missing folders are dropped)."""
    placeholders = ",".join("?" * len(names))
    out = {}
    for fid, name in conn.execute(
        f"SELECT id, name FROM folders WHERE name IN ({placeholders})", names
    ):
        out[name] = fid
    return out


def moe_inbox_last_sync(conn: sqlite3.Connection, inbox_ids: list[str]) -> datetime | None:
    """Return the most recent mail sync time of the INBOX folder(s), or None."""
    newest = None
    for fid in inbox_ids:
        row = conn.execute(
            "SELECT last_sync_at FROM sync_state WHERE scope = ?", (f"mail:{fid}",)
        ).fetchone()
        if row and row[0]:
            ts = datetime.fromtimestamp(row[0], tz=timezone.utc).replace(tzinfo=None)
            if newest is None or ts > newest:
                newest = ts
    return newest


def moe_message_to_paper(
    subject: str, sender: str, ts: int | None, rfc822: bytes | None, body_text: str | None
) -> dict | None:
    """Convert one moe message row into the standard paper dict.

    Prefers the full raw RFC 822 message (parsed with the same MIME logic
    as the mbox source) when its Subject header actually matches the row;
    falls back to the cached searchable body text, which is plain text of
    the form "title  URL  ^^ comment".
    """
    if not subject.startswith("[paper]"):
        return None
    title = subject[len("[paper]"):].strip()
    if not title:
        return None

    sender_email = sender or ""
    m = re.search(r"<([^>]+)>", sender_email)
    if m:
        sender_email = m.group(1)

    date_iso = ""
    if ts:
        try:
            date_iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except (ValueError, OSError, OverflowError):
            date_iso = ""

    if rfc822 and _rfc822_subject_matches(rfc822, subject):
        try:
            text = rfc822.decode("utf-8", errors="replace")
        except Exception:
            text = rfc822.decode("latin-1", errors="replace")
        parts = re.split(r"\r?\n\r?\n", text, maxsplit=1)
        if len(parts) >= 2:
            header_section, body_section = parts[0], parts[1]
            header_lines = header_section.split("\n", 1)
            if header_lines[0].startswith("From "):
                header_section = header_lines[1] if len(header_lines) > 1 else ""
            try:
                msg = process_message(header_section, body_section)
            except Exception:
                msg = None
            if msg:
                return msg

    if not body_text:
        return None
    url = extract_url_from_text(body_text)
    # moe can associate another message's cached body with this row. When
    # the body text contains neither the paper title nor a confident paper
    # URL, treat it as missing (the caller's carry-forward fills it in
    # from the previous run) instead of extracting garbage.
    title_key = re.sub(r"\s+", " ", title).strip().lower()
    body_key = re.sub(r"\s+", " ", body_text).strip().lower()
    body_belongs = bool(url) or (
        len(title_key) > 15 and title_key[:60] in body_key
    )
    if not body_belongs:
        return {
            "title": _clean_text(title),
            "url": "",
            "from": _clean_text(sender_email),
            "date": date_iso,
            "date_parsed": date_iso,
            "comment": "",
            "keywords": "",
        }
    out_title, comment = extract_title_and_comment(body_text, title, url)
    return {
        "title": _clean_text(out_title),
        "url": _clean_url(url),
        "from": _clean_text(sender_email),
        "date": date_iso,
        "date_parsed": date_iso,
        "comment": _clean_text(comment),
        "keywords": "",
    }


def _rfc822_subject_matches(rfc822: bytes, subject: str) -> bool:
    """True when the raw Subject header of the message equals the row subject.

    moe can associate an rfc822 blob with the wrong message row, so the
    blob is only trusted when its own Subject header (RFC 2047-decoded)
    matches the subject column.
    """
    try:
        text = rfc822.decode("utf-8", errors="replace")
    except Exception:
        return False
    m = re.search(r"\nSubject: ([^\r\n]*)", text)
    if not m:
        return False
    raw = m.group(1)
    try:
        decoded = ""
        for part, enc in decode_header(raw):
            decoded += part.decode(enc or "utf-8", errors="replace") if isinstance(part, bytes) else part
    except Exception:
        decoded = raw
    return (
        re.sub(r"\s+", " ", decoded).strip().lower()
        == re.sub(r"\s+", " ", subject).strip().lower()
    )


# Hosts that never carry the paper itself (mail-client footers, links to
# lab/organization homepages, meeting tools, ...). URLs on these hosts are
# skipped when picking the paper URL out of plain-text bodies.
_JUNK_URL_HOSTS = (
    "aka.ms", "microsoft.com", "outlook.com", "office.com", "live.com",
    "onedrive.com", "1drv.ms", "cemm.at", "gv.at", "fwf.ac.at", "ffg.at",
    "meduniwien.ac.at", "univie.ac.at", "oeaw.ac.at",
)

# Publisher/archive hosts, best first. Anything else scores below the
# confidence threshold (see extract_url_from_text).
_PRIORITY_HOSTS = (
    ("doi.org", 100), ("dx.doi.org", 100),
    ("pubmed.ncbi.nlm.nih.gov", 95),
    ("nature.com", 90), ("science.org", 90), ("cell.com", 90),
    ("sciencedirect.com", 90), ("pnas.org", 90), ("nejm.org", 90),
    ("jci.org", 90), ("elifesciences.org", 90),
    ("biorxiv.org", 85), ("medrxiv.org", 85), ("arxiv.org", 85),
    ("openreview.net", 85),
    ("plos.org", 80), ("frontiersin.org", 80), ("mdpi.com", 80),
    ("academic.oup.com", 80), ("oup.com", 80), ("link.springer.com", 80),
    ("springer.com", 80), ("wiley.com", 80), ("tandfonline.com", 80),
    ("sagepub.com", 80), ("annualreviews.org", 80), ("rupress.org", 80),
    ("embopress.org", 80), ("bmj.com", 80), ("thelancet.com", 80),
    ("liebertpub.com", 80), ("genome.gov", 80), ("researchsquare.com", 80),
    ("biomedcentral.com", 80), ("ahajournals.org", 80),
    ("bloodjournal.org", 80), ("jimmunol.org", 80), ("peerj.com", 80),
    ("karger.com", 80), ("jamanetwork.com", 80), ("huggingface.co", 75),
    ("zenodo.org", 75), ("osf.io", 75), ("figshare.com", 75),
    ("github.com", 70),
)

# URLs scoring below this are not paper URLs (mail-client footers, links
# to lab/organization homepages, meeting tools, ...) and are dropped.
_CONFIDENT_URL_SCORE = 60


def extract_url_from_text(body: str) -> str:
    """Pick the paper URL out of a plain-text body, or return "".

    moe's cached searchable body text is plain text: hyperlink-only paper
    URLs are lost, while footers/signatures contribute lots of irrelevant
    URLs. This unwraps Outlook safelinks and keeps only URLs on known
    publisher/archive hosts (or with a DOI pattern); anything else is
    dropped so the carry-forward from the previous run can take over.
    """
    unwrapped = re.sub(
        r"https?://eur\d*\.safelinks\.protection\.outlook\.com/\?url=([^&\"\s<]+)",
        lambda m: unquote(m.group(1)),
        body,
    )
    urls = re.findall(r"https?://[^\s<>\"')\]]+", unwrapped)
    best = ""
    best_score = -1
    for u in urls:
        u = _clean_url(html_mod.unescape(u))
        u = re.sub(r"^[^a-zA-Z]*", "", u)
        if not u.startswith(("http://", "https://")):
            continue
        host = (urlparse(u).hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if any(junk in host for junk in _JUNK_URL_HOSTS):
            continue
        score = 10
        for ph, sc in _PRIORITY_HOSTS:
            if host == ph or host.endswith("." + ph):
                score = sc
                break
        if score == 10 and re.search(r"10\.\d{4,9}/", u):
            score = 75
        if score > best_score:
            best_score = score
            best = u
    return best if best_score >= _CONFIDENT_URL_SCORE else ""



def extract_from_moe(
    db_path: Path, inbox_folders: list[str], sent_folders: list[str]
) -> tuple[list[dict], datetime | None]:
    """Extract [paper] messages from the moe SQLite store.

    Returns (papers, inbox_last_sync) where inbox_last_sync is the most
    recent sync time of the INBOX folder (used by the staleness guard).
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        folder_ids = moe_folder_ids(conn, inbox_folders + sent_folders)
        inbox_ids = [folder_ids[n] for n in inbox_folders if n in folder_ids]

        papers: list[dict] = []
        for name in inbox_folders + sent_folders:
            fid = folder_ids.get(name)
            if fid is None:
                log.info("moe folder %r not found, skipping", name)
                continue
            is_sent = name in sent_folders
            rows = conn.execute(
                """
                SELECT m.subject, m.from_, m.date, m.rfc822, fts.body
                FROM messages m
                LEFT JOIN message_fts fts ON fts.rowid = m.rowid
                WHERE m.folder_id = ? AND m.subject LIKE '[paper]%'
                """,
                (fid,),
            ).fetchall()
            folder_papers = 0
            for subject, sender, ts, rfc822, body_text in rows:
                msg = moe_message_to_paper(subject, sender, ts, rfc822, body_text)
                if msg:
                    msg["_sent"] = is_sent
                    papers.append(msg)
                    folder_papers += 1
            log.info("moe %s: extracted %d papers from %d tagged messages",
                     name, folder_papers, len(rows))

        last_sync = moe_inbox_last_sync(conn, inbox_ids)
        return papers, last_sync
    finally:
        conn.close()




# ── Index building (fast grep on the legacy mbox) ─────────────────────


def _index_signature(mbox_path: Path) -> str:
    """A header line identifying the mbox an index was built from."""
    st = mbox_path.stat()
    return (
        f"# mbox: {mbox_path.resolve()} | size: {st.st_size} "
        f"| mtime: {st.st_mtime_ns}\n"
    )


def build_indexes(mbox_path: Path, offsets_path: Path, lines_path: Path):
    """Run grep to build message-boundary and paper-subject indexes for one mbox."""
    sig = _index_signature(mbox_path)
    log.info("Building message boundary index for %s ...", mbox_path.name)
    with open(offsets_path, "w") as f:
        f.write(sig)
    subprocess.run(
        ["grep", "-nb", "^From ", str(mbox_path)],
        stdout=open(offsets_path, "a"),
        stderr=subprocess.DEVNULL,
        check=True,
    )
    log.info("Built %s (%d lines)", offsets_path.name, offsets_path.stat().st_size)

    log.info("Building paper subject index for %s ...", mbox_path.name)
    with open(lines_path, "w") as f:
        f.write(sig)
    subprocess.run(
        ["grep", "-n", "^Subject: \\[paper\\]", str(mbox_path)],
        stdout=open(lines_path, "a"),
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
            if not line or line.startswith("#"):
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
            if not line or line.startswith("#"):
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
            if end_byte - start_byte > MAX_MESSAGE_BYTES:
                log.warning(
                    "%s: skipping implausible byte range %d-%d "
                    "(> %d bytes); index likely corrupt",
                    mbox_path.name, start_byte, end_byte, MAX_MESSAGE_BYTES,
                )
                return "", None
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


def _clean_url(url: str) -> str:
    """Drop trailing whitespace/entities that glom onto URLs in HTML bodies
    (e.g. a literal `&nbsp;` entity decoded to a non-breaking space)."""
    return re.sub(r"[\s\u200b]+$", "", url)


def _clean_text(text: str) -> str:
    """Remove decoding/HTML artifacts from free-text fields: zero-width
    spaces, non-breaking spaces (-> regular space) and U+FFFD replacement
    characters left by lossy charset decoding."""
    if not text:
        return text
    text = text.replace("\u200b", "")
    text = text.replace("\u00a0", " ")
    return text.replace("\ufffd", "").strip()


def extract_originalsrc(body: str) -> str | None:
    for m in re.finditer(r'originalsrc="([^"]+)"', body):
        url = html_mod.unescape(m.group(1))
        url = _clean_url(url)
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
    return _clean_url(unquote(m.group(1))) if m else None


def extract_direct_href(body: str) -> str | None:
    m = re.search(r'<a\s[^>]*href="([^"]+)"', body)
    if m:
        url = _clean_url(m.group(1))
        if url.startswith(("http://", "https://")):
            return url
    return None


def extract_plain_url(body: str) -> str | None:
    """Fallback: find any http(s) URL in plain text."""
    for m in re.finditer(r"https?://[^\s<>\"']{10,}", body):
        url = m.group(0)
        if "safelinks.protection.outlook.com" in url:
            continue
        url = _clean_url(html_mod.unescape(url))
        # Strip leading non-URL junk from QP artifacts (e.g. "=https://" -> "https://")
        url = re.sub(r"^[^a-zA-Z]*", "", url)
        if url.startswith(("http://", "https://")):
            return url
    return None


def extract_direct_href(body: str) -> str | None:
    """Extract the first http(s) href from an <a> tag."""
    for m in re.finditer(r'<a\s[^>]*href="([^"]+)"', body, re.IGNORECASE):
        url = _clean_url(html_mod.unescape(m.group(1)))
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
                text = re.sub(r"https?://\S+", "", text)
                text = re.sub(r"\bwww\.\S+", "", text)
                text = re.sub(r"mailto:\S+", "", text)
                if not text.strip():
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
        line = re.sub(r"https?://\S+", "", line)
        line = re.sub(r"\bwww\.\S+", "", line)
        line = re.sub(r"mailto:\S+", "", line)
        if not line.strip():
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
        line = re.sub(r"https?://\S+", "", line).strip()
        if not line:
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
        "title": _clean_text(title),
        "url": _clean_url(url),
        "from": _clean_text(from_addr or ""),
        "date": date_str or "",
        "date_parsed": date_parsed,
        "comment": _clean_text(comment),
        "keywords": _clean_text(extract_header(header_section, "Keywords") or ""),
    }


def _indexes_stale(mbox_path: Path, offsets_path: Path, lines_path: Path) -> bool:
    """Return True if indexes are missing, older than the mbox, or built from
    a different mailbox (same paths can be reused after a switch)."""
    if not offsets_path.exists() or not lines_path.exists():
        return True
    mbox_mtime = mbox_path.stat().st_mtime
    if mbox_mtime > offsets_path.stat().st_mtime or mbox_mtime > lines_path.stat().st_mtime:
        return True
    sig = _index_signature(mbox_path)
    for idx_path in (offsets_path, lines_path):
        with idx_path.open() as f:
            header = f.readline()
        if header != sig:
            log.warning("Index %s was built for a different mailbox, rebuilding", idx_path.name)
            return True
    return False


def extract_papers(
    mbox_path: Path, offsets_path: Path, lines_path: Path
) -> list[dict]:
    """Build/load indexes and extract [paper] messages from one mbox."""
    if _indexes_stale(mbox_path, offsets_path, lines_path):
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
    # Load the previous run so we don't need to re-enrich, and so fields
    # moe can no longer provide (hyperlink-only URLs, some comments) are
    # carried forward instead of silently degrading.
    old_rows: dict[str, dict] = {}
    if OUTPUT.exists():
        with open(OUTPUT) as f:
            for row in csv.DictReader(f):
                key = row.get("title", "").strip().lower()
                if key and key not in old_rows:
                    old_rows[key] = row
        log.info("Loaded %d papers from the previous run", len(old_rows))

    all_papers: list[dict] = []
    inbox_last_sync: datetime | None = None

    moe_source = resolve_moe()
    if moe_source is not None:
        db_path, inbox_folders, sent_folders = moe_source
        log.info(
            "moe database: %s (inbox=%s, sent=%s)",
            db_path, inbox_folders, sent_folders,
        )
        all_papers, inbox_last_sync = extract_from_moe(
            db_path, inbox_folders, sent_folders
        )
    else:
        for label, mbox_path, offsets_path, lines_path in resolve_mailboxes():
            log.info("mbox: %s (%s)", mbox_path, mbox_path.stat().st_size)
            extracted = extract_papers(mbox_path, offsets_path, lines_path)
            all_papers.extend(extracted)

    # Deduplicate by title (case-insensitive); when the same paper appears
    # in several folders, keep the occurrence with the most content (a URL
    # beats no URL, a sent-message body beats an inbox one, a comment beats
    # no comment).
    seen: dict[str, int] = {}
    deduped: list[dict] = []
    for p in all_papers:
        key = p["title"].strip().lower()
        if not key:
            continue
        if key in seen:
            prev = deduped[seen[key]]
            if not prev.get("_sent") and p.get("_sent"):
                deduped[seen[key]] = p
            elif not prev["url"] and p["url"]:
                deduped[seen[key]] = p
            elif not prev["comment"] and p["comment"]:
                deduped[seen[key]] = p
            continue
        seen[key] = len(deduped)
        deduped.append(p)

    n_dup = len(all_papers) - len(deduped)
    log.info("Total extracted: %d, duplicates removed: %d, final: %d",
             len(all_papers), n_dup, len(deduped))

    # Staleness guard: refuse to proceed when the mailbox was not synced
    # recently. papers.csv is rebuilt from scratch below, so proceeding on a
    # stale mailbox would silently drop papers.
    if os.environ.get("ALLOW_STALE_MAIL") != "1":
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if inbox_last_sync is not None:
            if (now - inbox_last_sync).total_seconds() > 7 * 86400:
                log.error(
                    "moe mailbox has not synced in the last 7 days "
                    "(last sync: %s). Run 'moe sync' (or the TUI) and let it "
                    "finish, then re-run. Override with ALLOW_STALE_MAIL=1 if "
                    "you are sure no [paper] emails are missing.",
                    inbox_last_sync.isoformat(),
                )
                sys.exit(1)
        else:
            newest: datetime | None = None
            for p in deduped:
                raw = p.get("date_parsed", "")
                if not raw:
                    continue
                try:
                    ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if ts.tzinfo is not None:
                    ts = ts.astimezone(timezone.utc).replace(tzinfo=None)
                if newest is None or ts > newest:
                    newest = ts
            if newest is None or (now - newest).total_seconds() > 7 * 86400:
                newest_s = newest.isoformat() if newest is not None else "none"
                log.error(
                    "No [paper]-tagged email found in the last 7 days (newest: %s). "
                    "The mailbox looks stale - sync the account, then re-run. "
                    "Override with ALLOW_STALE_MAIL=1 if you are sure no "
                    "[paper] emails are missing.",
                    newest_s,
                )
                sys.exit(1)

    # Merge back data from the previous run: enrichment (doi/journal) and
    # fields the moe store no longer exposes (hyperlink-only URLs, comments
    # lost or polluted in the plain-text body cache). Inbox-message bodies
    # in moe are unreliable, so for papers whose occurrence comes from the
    # inbox, the previous run's URL and comment win when they exist.
    for p in deduped:
        key = p["title"].strip().lower()
        prev = old_rows.get(key, {})
        if prev.get("doi") or prev.get("journal"):
            p["doi"] = prev.get("doi", "")
            p["journal"] = prev.get("journal", "")
        if moe_source is not None and not p.get("_sent"):
            if prev.get("url") and prev["url"] != p.get("url", ""):
                p["url"] = prev["url"]
            if prev.get("comment") and prev["comment"] != p.get("comment", ""):
                p["comment"] = prev["comment"]
        else:
            if not p["url"] and prev.get("url"):
                p["url"] = prev["url"]
            if not p["comment"] and prev.get("comment"):
                p["comment"] = prev["comment"]

    # Papers that vanished from the mail store (e.g. moe rows with an empty
    # cached body) are restored from the previous run instead of dropped.
    missing_keys = set(old_rows) - {p["title"].strip().lower() for p in deduped}
    if missing_keys:
        for key in sorted(missing_keys):
            row = old_rows[key]
            row["_sent"] = False
            deduped.append(row)
        log.info("Restored %d papers missing from the mail store", len(missing_keys))

    # Sort deterministically (date, then title) so that papers.csv is
    # stable across runs and diffs only show genuinely new/changed rows.
    deduped.sort(
        key=lambda p: (p.get("date_parsed", ""), p.get("title", "").lower())
    )

    with open(OUTPUT, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "title", "url", "from", "date", "date_parsed",
                "comment", "keywords", "doi", "journal",
            ],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(
            {k: v for k, v in p.items() if k != "_sent"} for p in deduped
        )

    log.info("Wrote %s (%d rows)", OUTPUT, len(deduped))


if __name__ == "__main__":
    main()
