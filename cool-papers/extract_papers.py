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


def resolve_mailboxes(required: bool = True) -> list[tuple[str, Path, Path, Path]]:
    """Resolve mbox sources from env vars or config.yaml.

    When `required`, the INBOX mailbox is mandatory (no built-in default, so
    a stale or missing configuration fails loudly instead of silently using
    a wrong path); otherwise an empty list is returned when it is missing.
    The sent mailbox is optional.

    Returns a list of (label, mbox_path, offsets_path, lines_path).
    """
    cfg = load_config()
    inbox_raw = os.environ.get("MAILBOX") or cfg.get("mailbox")
    if not required and (not inbox_raw or not Path(inbox_raw).expanduser().exists()):
        return []
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
    subject: str,
    sender: str,
    ts: int | None,
    rfc822: bytes | None,
    body_text: str | None,
    is_sent: bool = False,
) -> dict | None:
    """Convert one moe message row into the standard paper dict.

    Prefers the full raw RFC 822 message (parsed with the same MIME logic
    as the mbox source) when its Subject header actually matches the row;
    falls back to the cached searchable body text, which is plain text of
    the form "title  URL  ^^ comment".

    The returned dict has `_trusted` set when its URL/comment come from a
    body known to belong to this message (matching rfc822 blob, a sent
    message, or an inbox body that contains the title).
    """
    if not subject.startswith("[paper]"):
        return None
    title = clean_title(subject[len("[paper]"):])
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
                msg["_trusted"] = True
                return msg

    if not body_text:
        return None
    # moe can associate another message's cached body with an inbox row
    # (sent-folder bodies have been reliable). An inbox body that does not
    # contain the paper title is treated as missing (the caller's
    # carry-forward fills it in from the previous run) instead of
    # extracting garbage such as a newsletter's links.
    tkey = re.sub(r"\s+", " ", title).strip().lower()
    body_key = re.sub(r"\s+", " ", body_text).strip().lower()
    body_belongs = is_sent or (len(tkey) > 15 and tkey[:60] in body_key)
    if not body_belongs:
        return {
            "title": clean_title(title),
            "url": "",
            "from": _clean_text(sender_email),
            "date": date_iso,
            "date_parsed": date_iso,
            "comment": "",
            "keywords": "",
            "_trusted": False,
        }
    url = extract_url_from_text(body_text)
    out_title, comment = extract_title_and_comment(body_text, title, url)
    return {
        "title": clean_title(out_title),
        "url": _clean_url(url),
        "from": _clean_text(sender_email),
        "date": date_iso,
        "date_parsed": date_iso,
        "comment": _clean_text(comment),
        "keywords": "",
        "_trusted": True,
    }


def moe_find_body(conn: sqlite3.Connection, title: str) -> str | None:
    """Find a cached message body that starts with the paper title.

    moe can attach a message's body to another row; the full-text index is
    searched for the title and a body is accepted only when the title
    appears at its very beginning (as in a shared-paper email, not in a
    reply quoting it).
    """
    words = re.findall(r"\w+", title)[:10]
    if len(words) < 3:
        return None
    tkey = re.sub(r"\s+", " ", title).strip().lower()[:60]
    query = '"' + " ".join(words) + '"'
    try:
        rows = conn.execute(
            "SELECT body FROM message_fts WHERE message_fts MATCH ? LIMIT 20",
            (query,),
        ).fetchall()
    except sqlite3.Error:
        return None
    for (body,) in rows:
        head = re.sub(r"\s+", " ", body or "").strip().lower()[:200]
        if tkey in head and "reacted to your message" not in head:
            return body
    return None


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
    "meduniwien.ac.at", "univie.ac.at", "oeaw.ac.at", "andre-rendeiro.com",
    "sharepoint.com",
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
    ("karger.com", 80), ("jamanetwork.com", 80),
    # Code/data hosts are below the confidence threshold: they are only
    # picked when the message has no publisher URL (they are usually linked
    # in the comment, next to the paper)
    ("huggingface.co", 50), ("zenodo.org", 50), ("osf.io", 50),
    ("figshare.com", 50), ("github.com", 50),
)

# URLs scoring below this are not paper URLs (mail-client footers, links
# to lab/organization homepages, meeting tools, ...) and are dropped.
_CONFIDENT_URL_SCORE = 60


def extract_url_from_text(body: str) -> str:
    """Pick the paper URL out of a plain-text body, or return "".

    moe's cached searchable body text is plain text: hyperlink-only paper
    URLs are lost, while footers/signatures contribute lots of irrelevant
    URLs. Outlook safelinks are unwrapped and the signature/quoted reply is
    cut off before scoring (see _pick_url).
    """
    text = _cut_signature_and_quote(_unwrap_safelinks(body))
    return _pick_url(_URL_RE.findall(text))


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
                msg = moe_message_to_paper(
                    subject, sender, ts, rfc822, body_text, is_sent
                )
                if msg and not msg.get("_trusted"):
                    # The row's cached body belongs to another message; the
                    # right body is often attached to a different row
                    other = moe_find_body(conn, msg["title"])
                    if other:
                        msg = moe_message_to_paper(subject, sender, ts, None, other)
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
        f"| mtime: {st.st_mtime_ns} | v2\n"
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
    # Subjects with non-ASCII characters are RFC 2047-encoded; "[paper]" is
    # then literal or "=5Bpaper=5D" (Q encoding) or "W3BhcGVyX" (base64)
    subprocess.run(
        [
            "grep", "-n", "-E",
            r"^Subject: (\[paper\]|=\?[^?]+\?[QqBb]\?(\[paper\]|=5[Bb]paper=5[Dd]|W3BhcGVyX))",
            str(mbox_path),
        ],
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


def _decode_mime_header(value: str) -> str:
    """Decode RFC 2047 encoded words ("=?utf-8?q?...?=")."""
    if "=?" not in value:
        return value
    try:
        out = ""
        for part, enc in decode_header(value):
            out += part.decode(enc or "utf-8", errors="replace") if isinstance(part, bytes) else part
        return re.sub(r"\s+", " ", out).strip()
    except Exception:
        return value


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
                return _decode_sub_body(sub_body, cte, _charset(ct))
            if "text/plain" in ct and "text/html" not in (
                extract_header(body_section[:3000], "Content-Type")
            ):
                return _decode_sub_body(sub_body, cte, _charset(ct))
        return None

    # Not multipart – use main-header CTE
    decoded = body_section
    if content_transfer_encoding:
        decoded = _decode_sub_body(
            decoded, content_transfer_encoding, _charset(content_type)
        )

    # Strip trailing mbox "From " separator if splice was imprecise
    decoded = re.sub(r"\nFrom[ -].*", "\n", decoded, count=1).strip()
    return decoded


def _charset(content_type: str) -> str:
    """The charset parameter of a Content-Type header (default utf-8)."""
    m = re.search(r'charset="?([^"\s;]+)', content_type, re.IGNORECASE)
    return m.group(1) if m else "utf-8"


def _decode_bytes(raw: bytes, charset: str) -> str:
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _decode_sub_body(text: str, encoding: str, charset: str = "utf-8") -> str:
    """Decode a sub-part according to its Content-Transfer-Encoding and charset."""
    if "base64" in encoding.lower():
        b64 = re.sub(r"\s", "", text.strip())
        try:
            return _decode_bytes(base64.b64decode(b64), charset)
        except Exception:
            return text
    if "quoted-printable" in encoding.lower():
        try:
            raw = quopri.decodestring(text.encode("ascii", errors="replace"))
            return _decode_bytes(raw, charset)
        except Exception:
            return text
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
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = text.replace("\u00a0", " ")
    return text.replace("\ufffd", "").strip()


# Parentheses are allowed inside URLs (Cell/Lancet PII URLs such as
# S0092-8674(25)00749-4); unbalanced trailing ones are trimmed by _trim_url.
_URL_RE = re.compile(r"https?://[^\s<>\"'\]]+", re.IGNORECASE)

_SAFELINK_RE = re.compile(
    r"https?://[\w.]*safelinks\.protection\.outlook\.com/\?url=([^&\"\s<>]+)[^\s<>\"]*",
    re.IGNORECASE,
)


def _unwrap_safelinks(text: str) -> str:
    """Replace Outlook safelinks with the URL they wrap."""
    return _SAFELINK_RE.sub(lambda m: unquote(m.group(1)), text)


def _trim_url(url: str) -> str:
    """Normalize a URL found in free text: decode entities, strip leading
    junk (QP artifacts such as "=https://"), trailing punctuation and
    unbalanced closing brackets, and lower-case the scheme ("Https://")."""
    url = _clean_url(html_mod.unescape(url))
    url = re.sub(r"^[^a-zA-Z]*", "", url)
    while url:
        last = url[-1]
        if last in ".,;:!?*'\"":
            url = url[:-1]
        elif last == ")" and url.count(")") > url.count("("):
            url = url[:-1]
        elif last == "]" and url.count("]") > url.count("["):
            url = url[:-1]
        else:
            break
    return re.sub(r"^https?://", lambda m: m.group(0).lower(), url, flags=re.IGNORECASE)


def _url_score(url: str) -> int:
    """Score a candidate paper URL; -1 for junk hosts."""
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host or any(junk in host for junk in _JUNK_URL_HOSTS):
        return -1
    for ph, sc in _PRIORITY_HOSTS:
        if host == ph or host.endswith("." + ph):
            return sc
    if re.search(r"10\.\d{4,9}/", url):
        return 75
    return 10


def _pick_url(candidates) -> str:
    """Return the paper URL among candidates given in document order.

    The highest-priority publisher/archive URL wins, the first one on ties
    (later ones are usually related work mentioned in the comment); when
    there is none, the first non-junk URL is returned (e.g. a project page
    shared as a "paper").
    """
    best, best_score = "", -1
    fallback = ""
    for u in candidates:
        u = _trim_url(u)
        if not u.startswith(("http://", "https://")):
            continue
        score = _url_score(u)
        if score >= _CONFIDENT_URL_SCORE and score > best_score:
            best, best_score = u, score
        elif score > 0 and not fallback:
            fallback = u
    return best or fallback


def extract_url(body: str) -> str:
    """Pick the paper URL out of an HTML or plain-text message body.

    Candidates come from Outlook's originalsrc attributes, <a href> links
    and plain-text URLs (all with safelinks unwrapped); the signature and
    any quoted reply are cut off first.
    """
    body = _cut_signature_and_quote(_unwrap_safelinks(_strip_style_script(body)))
    # Link targets and plain-text URLs, in document order
    candidates = []
    for piece in re.split(r"(<[^>]*>)", body):
        if piece.startswith("<"):
            candidates += re.findall(r'(?:originalsrc|href)="([^"]+)"', piece, re.IGNORECASE)
        else:
            candidates += _URL_RE.findall(html_mod.unescape(piece))
    return _pick_url(html_mod.unescape(c) for c in candidates)


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
        # No good body title found; keep the subject as the title
        return subject_title, _extract_comment_from_body(body, subject_title, url)

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


def _looks_like_html(body: str) -> bool:
    return bool(re.search(r"<(?:html|body|div|p|br|span|table)\b", body, re.IGNORECASE))


def _strip_signature(body: str) -> str:
    """Remove Outlook link-preview cards (page title, description and domain
    of a pasted link) from HTML body text.

    Outlook's "Signature" divs are kept: older Outlook versions put whatever
    was typed below the signature insertion point (often the comment) in
    them. Real signatures are cut by _cut_signature_and_quote instead.
    """
    if not BeautifulSoup or not _looks_like_html(body):
        return body
    try:
        soup = BeautifulSoup(body, "html.parser")
        for selector in (
            "[id^=LPBorder]", "[id^=LPContainer]", "[class*=_EType_OWALinkPreview]",
            "[class*=lp-rich-link]",
        ):
            for elem in soup.select(selector):
                elem.decompose()
        return str(soup)
    except Exception:
        return body


# Lines that end the useful part of a message: sign-offs, signatures,
# mobile footers and quoted replies/forwards. Everything from the first
# match onwards is dropped. Each pattern must match a whole line (or a
# whole HTML element, hence the optional surrounding tags).
_CUT_LINE_PATTERNS = (
    r"(?:best|best wishes|best regards|kind regards|warm regards|regards|"
    r"cheers|all the best|many thanks|thanks a lot|thanks|thank you|bw|br|"
    r"liebe grüße|lg|viele grüße)\s*[,!.]?",
    r"andre rendeiro, phd.*",
    r"cemm research center for molecular medicine.*",
    r"lazarettgasse.*",
    r"[\w.-]+(?: [\w.-]+){1,3}, (?:msc|bsc|phd|ph\.d\.|md|dr)\b.*",
    r"sent from my .*",
    r"get outlook for .*",
    r"-{3,}\s*original message\s*-{3,}.*",
    r"-{5,}\s*forwarded message\s*-{5,}.*",
    r"_{10,}",
    r"\*?(?:from|von):\*?\s.*",
    r"on .{5,120} wrote:",
    r"am .{5,120} schrieb.*",
    r"\d{4}年.*",
)
_CUT_RE = re.compile(
    r"(?im)^(?:\s|<[^>]*>|&nbsp;)*(?:" + "|".join(_CUT_LINE_PATTERNS) + r")"
    r"(?:\s|<[^>]*>|&nbsp;)*$"
)
# Outlook HTML reply/forward containers
_CUT_HTML_RE = re.compile(r'<(?:div|hr)[^>]*id="(?:divRplyFwdMsg|appendonsend)"', re.IGNORECASE)


def _cut_signature_and_quote(body: str) -> str:
    """Drop everything from the first sign-off, signature or quoted reply."""
    cut = len(body)
    for rx in (_CUT_RE, _CUT_HTML_RE):
        m = rx.search(body)
        if m:
            cut = min(cut, m.start())
    # Plain-text quoted reply (lines starting with ">")
    m = re.search(r"(?m)^>", body)
    if m:
        cut = min(cut, m.start())
    return body[:cut]


_BLOCK_TAG_RE = re.compile(
    r"<(?:br|hr)\b[^>]*>|</?(?:div|p|li|ul|ol|tr|table|h\d|blockquote)\b[^>]*>",
    re.IGNORECASE,
)


def _html_to_text(body: str) -> str:
    """Convert HTML to text with line breaks only at block-level elements
    (inline elements such as links or italics stay within their line)."""
    m = _CUT_HTML_RE.search(body)
    if m:
        body = body[: m.start()]
    body = re.sub(r"\s+", " ", body)
    body = _BLOCK_TAG_RE.sub("\n", body)
    if BeautifulSoup:
        try:
            text = BeautifulSoup(body, "html.parser").get_text()
        except Exception:
            text = re.sub(r"<[^>]+>", "", body)
    else:
        text = re.sub(r"<[^>]+>", "", body)
    return text


def _space_glued_urls(text: str) -> str:
    """Separate URLs glued to the preceding word (adjacent inline elements:
    "...title</span><a>https://...")."""
    return re.sub(r"(?<=[^\s(<\[\"'=/])(https?://)", r" \1", text)


_GREETING_RE = re.compile(r"(?i)^(?:dear|hi|hello|hey|hallo|liebe|lieber)\b[^.?!]{0,60}[,!:]?$")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower().strip(" .,:;-–|")


def _extract_comment_from_body(body: str, title: str, url: str) -> str:
    """Return body text that is not the title, URL, greeting or boilerplate.

    The signature and any quoted reply are cut off first. URLs other than
    the paper URL are kept, since comments often point to related papers,
    code or data.
    """
    if not body:
        return ""

    body = _unwrap_safelinks(_strip_style_script(body))
    text = _html_to_text(body) if _looks_like_html(body) else body
    text = _cut_signature_and_quote(html_mod.unescape(text))
    # Plain-text renderings of hyperlinks ("link text<https://...>") and of
    # inline images ("[https://.../image.png]")
    text = re.sub(r"<(?:https?|mailto):[^>\s]*>", "", text)
    text = re.sub(r"\[(?:https?|cid):[^\]\s]*\]", "", text)
    text = _space_glued_urls(text)
    # The title on its own line(s), possibly wrapped and followed by a
    # site suffix ("... - Nature Medicine")
    words = [re.escape(w) for w in title.split()]
    if words:
        text = re.sub(
            r"(?im)^\s*" + r"\s+".join(words)
            + r"(?:\s*[-|:–]\s*(?:The Lancet|Nature|Cell|Science|ScienceDirect|bioRxiv"
            r"|medRxiv|PubMed|Modern Pathology)[\w ]*)?[.\s]*$",
            "",
            text,
        )

    title_norm = _norm(title)
    url_norm = _trim_url(url).rstrip("/") if url else ""

    lines = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or _GREETING_RE.match(line):
            continue
        # Drop the paper URL wherever it appears (also when wrapped over
        # two lines in the HTML source)
        if url_norm:
            line = " ".join(
                w for w in line.split()
                if not (
                    w.lower().startswith("http")
                    and url_norm.lower().startswith(_trim_url(w).rstrip("/").lower())
                )
            )
        line_norm = _norm(line)
        if len(line_norm) < 3 or sum(c.isalpha() for c in line_norm) < 3:
            continue
        # Plain-text link previews end with the bare domain ("www.cell.com"),
        # after the page title (already dropped) and description
        if re.fullmatch(r"(?:www\.)?[\w-]+(?:\.[\w-]+)+/?", line_norm):
            if lines and "^" not in lines[-1]:
                lines.pop()
            continue
        # The title (when it differs slightly from the subject)
        if title_norm and line_norm == title_norm:
            continue
        if line_norm in ("book time to meet with me", "reply", "nbsp", "=20"):
            continue
        lines.append(line)

    # A bare first name signing off the message
    while lines and re.fullmatch(r"[A-ZÀ-Ý][\w-]{1,15}", lines[-1]):
        lines.pop()

    comment = re.sub(r"\s+", " ", " ".join(lines)).strip()
    if _norm(comment) == title_norm:
        return ""
    return comment


_TITLE_SUFFIX_RE = re.compile(
    r"\s*(?::\s*(?:Cell|Neuron|Immunity|Cancer Cell|Molecular Cell|Developmental Cell"
    r"|Cell (?:Reports|Metabolism|Systems|Genomics|Stem Cell)[\w ]*)"
    r"|\s[-|–]\s(?:The Lancet[\w ]*|Modern Pathology|ScienceDirect|PubMed|bioRxiv|medRxiv"
    r"|Nature[\w ]*|Science[\w ]*))$"
)


def clean_title(title: str) -> str:
    """Strip browser-tab site suffixes pasted along with the title
    ("...: Cell", "... - The Lancet Digital Health", "... - ScienceDirect")."""
    title = _clean_text(title or "")
    return _TITLE_SUFFIX_RE.sub("", title).strip()


def title_key(title: str) -> str:
    """Key used to deduplicate papers and match them across runs."""
    return re.sub(r"\s+", " ", clean_title(title)).lower()


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
    subj = _decode_mime_header(extract_header(header_section, "Subject"))
    if not subj.startswith("[paper]") or subj.upper().startswith("RE:"):
        return None

    title = clean_title(subj[len("[paper]"):])
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
        "title": clean_title(title),
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


# Quoted-reply markers inside a single-line comment from a previous run
_INLINE_QUOTE_RE = re.compile(
    r"\s(?:From|Von|Sent|Gesendet):\s|\s\d{4}年|\s>\s|\sOn .{5,120} wrote:|\sAm .{5,120} schrieb"
)


def _clean_carried_comment(comment: str) -> str:
    """Trim quoted replies from a comment carried over from a previous run
    (older runs did not cut them)."""
    m = _INLINE_QUOTE_RE.search(comment)
    return comment[: m.start()].strip() if m else comment


def _source_rank(p: dict) -> tuple:
    """Rank duplicate occurrences of a paper: one with a URL beats one
    without; a body known to belong to the message beats an unverified one;
    a comment beats none; the raw mbox archive (full MIME, hyperlinks
    intact) beats moe's cached plain-text bodies; sent beats inbox."""
    return (
        bool(p.get("url")),
        bool(p.get("_trusted")),
        bool(p.get("comment")),
        bool(p.get("_mbox")),
        bool(p.get("_sent")),
    )


def main():
    # Load the previous run so we don't need to re-enrich, and so fields
    # moe can no longer provide (hyperlink-only URLs, some comments) are
    # carried forward instead of silently degrading.
    old_rows: dict[str, dict] = {}
    if OUTPUT.exists():
        with open(OUTPUT) as f:
            for row in csv.DictReader(f):
                row["title"] = clean_title(row.get("title", ""))
                key = title_key(row["title"])
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
        # The legacy mbox archive, when still on disk, holds the raw
        # messages up to its last sync: use it as a reference for the
        # papers it contains (moe's cached bodies lose hyperlinks and can
        # belong to another message).
        mboxes = resolve_mailboxes(required=False)
    else:
        mboxes = resolve_mailboxes()
    for label, mbox_path, offsets_path, lines_path in mboxes:
        log.info("mbox: %s (%s)", mbox_path, mbox_path.stat().st_size)
        for p in extract_papers(mbox_path, offsets_path, lines_path):
            p["_sent"] = label == "sent"
            p["_mbox"] = True
            p["_trusted"] = True
            all_papers.append(p)

    # Deduplicate by title (case-insensitive); when the same paper appears
    # in several folders/sources, keep the best occurrence (_source_rank),
    # and the latest one among equally good occurrences.
    seen: dict[str, int] = {}
    deduped: list[dict] = []
    for p in all_papers:
        key = title_key(p["title"])
        if not key:
            continue
        if key in seen:
            prev = deduped[seen[key]]
            # A paper shared several times (e.g. preprint, then published
            # version) keeps its latest share on ties
            if _source_rank(p) > _source_rank(prev) or (
                _source_rank(p) == _source_rank(prev)
                and p.get("date_parsed", "") > prev.get("date_parsed", "")
            ):
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
    # lost or polluted in the plain-text body cache). When a paper's body
    # could not be verified to belong to its message (moe inbox rows), the
    # previous run's URL and comment win when they exist.
    for p in deduped:
        key = title_key(p["title"])
        prev = old_rows.get(key, {})
        if not p.get("_trusted"):
            if prev.get("url"):
                p["url"] = prev["url"]
            if prev.get("comment"):
                p["comment"] = _clean_carried_comment(prev["comment"])
        elif not p["url"] and prev.get("url"):
            # Plain-text bodies lose hyperlink-only URLs
            p["url"] = prev["url"]
        # Enrichment is only valid for the same URL (re-enriched otherwise)
        if (prev.get("doi") or prev.get("journal")) and prev.get("url") == p["url"]:
            p["doi"] = prev.get("doi", "")
            p["journal"] = prev.get("journal", "")

    # Papers that vanished from the mail store (e.g. moe rows with an empty
    # cached body) are restored from the previous run instead of dropped.
    missing_keys = set(old_rows) - {title_key(p["title"]) for p in deduped}
    if missing_keys:
        for key in sorted(missing_keys):
            row = old_rows[key]
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
            {k: v for k, v in p.items() if not k.startswith("_")} for p in deduped
        )

    log.info("Wrote %s (%d rows)", OUTPUT, len(deduped))


if __name__ == "__main__":
    main()
