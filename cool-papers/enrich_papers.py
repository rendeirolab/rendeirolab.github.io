#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "requests",
# ]
# ///

"""Enrich papers.csv with DOI and journal name via Crossref API + domain mapping.

Adds two columns: doi, journal

Usage:
    uv run enrich_papers.py
"""

import csv
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlparse

try:
    import requests
except ImportError:
    requests = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("enrich_papers")

SCRIPT_DIR = Path(__file__).parent.resolve()
CSV_PATH = SCRIPT_DIR / "papers.csv"

CROSSREF_API = "https://api.crossref.org/works/{doi}"


# ---------------------------------------------------------------------------
# DOI extraction from URL
# ---------------------------------------------------------------------------

def _clean_doi(doi: str) -> str:
    doi = doi.rstrip(".,;:/?#").split("#")[0].split("?")[0]
    return doi


def _doi_clean_g1(m: re.Match) -> str:
    return _clean_doi(m.group(1))


def _doi_clean_g2(m: re.Match) -> str:
    return _clean_doi(m.group(2))


def _doi_nature(m: re.Match) -> str:
    return "10.1038/" + m.group(1)


def _doi_cell_pii(m: re.Match) -> str:
    return "10.1016/j." + m.group(1)


def _doi_lancet(m: re.Match) -> str:
    raw = m.group(1).split("?")[0].split("#")[0].rstrip(".")
    return "10.1016/" + raw


def _doi_elife(m: re.Match) -> str:
    return "10.7554/eLife." + m.group(1)


def _doi_none(m: re.Match) -> None:
    return None


def _doi_genome(url: str, _: re.Match) -> str | None:
    m2 = re.search(r"/(10\.\d+/\S+)", url)
    return _clean_doi(m2.group(1)) if m2 else None


# (regex, handler, flags)
_DOI_PATTERNS: list[tuple[str, callable, int]] = [
    (r"doi\.org/(10\.\d+/\S+)", _doi_clean_g1, 0),
    (r"(biorxiv|medrxiv)\.org/content/(10\.\d+/\S+)", _doi_clean_g2, 0),
    (r"nature\.com/articles/([a-z0-9-]+)", _doi_nature, re.I),
    (r"science\.org/doi/(?:full/)?(10\.\d+/\S+)", _doi_clean_g1, 0),
    (r"pnas\.org/doi/(10\.\d+/\S+)", _doi_clean_g1, 0),
    (r"journals\.plos\.org/[^/]+/article\?id=(10\.\d+/\S+)", _doi_clean_g1, 0),
    (r"(?:onlinelibrary|advanced\.onlinelibrary)\.wiley\.com/doi/(10\.\d+/\S+)", _doi_clean_g1, 0),
    (r"link\.springer\.com/(?:article|chapter)/(10\.\d+/\S+)", _doi_clean_g1, 0),
    (r"biomedcentral\.com/articles/(10\.\d+/\S+)", _doi_clean_g1, 0),
    (r"cell\.com/[^/]+/(?:fulltext|abstract|pdf)/\S*?(\d{4}-\d+X?[^?#\s]*)", _doi_none, 0),
    (r"cell\.com/[^/]+/pdf/(\S+)\.pdf", _doi_none, 0),
    (r"sciencedirect\.com/science/article/(?:pii|abs/pii)/(\S+)", _doi_cell_pii, 0),
    (r"www-nature-com\.ez\.srv\.meduniwien\.ac\.at/articles/([a-z0-9-]+)", _doi_nature, re.I),
    (r"thelancet\.com/journals/[^/]+/article/(\S+)", _doi_lancet, 0),
    (r"elifesciences\.org/articles/(\d+)", _doi_elife, 0),
    (r"academic\.oup\.com/[^/]+/article(?:-abstract)?/doi/(10\.\d+/\S+)", _doi_clean_g1, 0),
    (r"academic\.oup\.com/[^/]+/article(?:-abstract)?/\d+/\d+/\d+/(\d+)", _doi_none, 0),
    (r"pubs\.rsna\.org/doi/(10\.\d+/\S+)", _doi_clean_g1, 0),
    (r"meridian\.allenpress\.com/[^/]+/doi/(10\.\d+/\S+)", _doi_clean_g1, 0),
    (r"journals\.biologists\.com/[^/]+/doi/(10\.\d+/\S+)", _doi_clean_g1, 0),
    (r"neurology\.org/doi/(10\.\d+/\S+)", _doi_clean_g1, 0),
]


def extract_doi(url: str) -> str | None:
    if not url.startswith("http"):
        return None
    for regex, handler, flags in _DOI_PATTERNS:
        m = re.search(regex, url, flags)
        if m:
            return handler(m)
    # Genome Medicine / BMC Genomics — has DOI somewhere in path
    if re.search(r"genome(?:medicine|biology)\.", url, re.I):
        return _doi_genome(url, None)
    return None


# ---------------------------------------------------------------------------
# Domain → journal lookup (for non-DOI URLs)
# ---------------------------------------------------------------------------

_NON_JOURNAL_DOMAINS: set[str] = {
    "github.com", "github.io",
    "linkedin.com",
    "schemas.microsoft.com",
    "storage.googleapis.com",
    "semanticscholar.org",
    "bioptimus.com",
    "bmc-compbio.github.io",
    "cemm.at",
}

_SIMPLE_DOMAIN_MAP: dict[str, str] = {
    "arxiv.org": "arXiv",
    "biorxiv.org": "bioRxiv",
    "medrxiv.org": "medRxiv",
    "substack.com": "Substack",
    "openreview.net": "OpenReview",
    "pathologyjournal.rcpa.edu.au": "Pathology",
    "pubmed.ncbi.nlm.nih.gov": "PubMed",
    "ncbi.nlm.nih.gov": "PubMed",
    "openaccess.thecvf.com": "CVF Open Access",
    "academic.oup.com": "Oxford University Press",
    "pnas.org": "PNAS",
    "thelancet.com": "The Lancet",
    "wiley.com": "Wiley",
    "springer.com": "Springer",
    "plos.org": "PLOS",
    "elifesciences.org": "eLife",
}

_CELL_PATH_MAP: list[tuple[str, str]] = [
    ("/cell/", "Cell"),
    ("/immunity/", "Immunity"),
    ("/neuron/", "Neuron"),
    ("/developmental-cell/", "Developmental Cell"),
    ("/devcell/", "Developmental Cell"),
    ("/cell-reports-medicine/", "Cell Reports Medicine"),
    ("/cell-reports-methods/", "Cell Reports Methods"),
    ("/cell-reports/", "Cell Reports"),
    ("/cell-stem-cell/", "Cell Stem Cell"),
    ("/cell-metabolism/", "Cell Metabolism"),
    ("/cancer-cell/", "Cancer Cell"),
    ("/iscience/", "iScience"),
    ("/molecular-plant/", "Molecular Plant"),
]

_SCIENCE_PATH_MAP: list[tuple[str, str]] = [
    ("/science/", "Science"),
    ("/sciadv/", "Science Advances"),
    ("/scienceadvances/", "Science Advances"),
    ("/sciimmunol/", "Science Immunology"),
    ("/scitranslmed/", "Science Translational Medicine"),
    ("/scirobotics/", "Science Robotics"),
    ("/scienceinsignal/", "Science Signaling"),
]

_NATURE_PATH_MAP: list[tuple[str, str]] = [
    ("/s41586", "Nature"),
    ("/nature", "Nature"),
    ("/s41591", "Nature Methods"),
    ("/nmeth", "Nature Methods"),
    ("/s41587", "Nature Biotechnology"),
    ("/nbt", "Nature Biotechnology"),
    ("/ng", "Nature Genetics"),
    ("/s41588", "Nature Genetics"),
    ("/s41593", "Nature Neuroscience"),
    ("/nrn", "Nature Neuroscience"),
    ("/s41576", "Nature Reviews Genetics"),
    ("/nrg", "Nature Reviews Genetics"),
    ("/s41590", "Nature Immunology"),
    ("/ni", "Nature Immunology"),
    ("/s41577", "Nature Reviews Immunology"),
    ("/nri", "Nature Reviews Immunology"),
    ("/s41467", "Nature Communications"),
    ("/ncomms", "Nature Communications"),
    ("/s41556", "Nature Cell Biology"),
    ("/s41580", "Nature Reviews Molecular Cell Biology"),
    ("/s41579", "Nature Reviews Microbiology"),
    ("/s41584", "Nature Reviews Disease Primers"),
    ("/s41574", "Nature Reviews Endocrinology"),
    ("/s41581", "Nature Reviews Nephrology"),
    ("/s41568", "Nature Reviews Cancer"),
    ("/s41571", "Nature Reviews Clinical Oncology"),
    ("/s41572", "Nature Reviews Drug Discovery"),
    ("/s41573", "Nature Reviews Drug Discovery"),
    ("/s41575", "Nature Reviews Gastroenterology & Hepatology"),
    ("/s41578", "Nature Reviews Urology"),
    ("/s41582", "Nature Reviews Neurology"),
    ("/s41583", "Nature Reviews Neuroscience"),
    ("/s41594", "Nature Reviews Methods Primers"),
    ("/s41596", "Nature Protocols"),
    ("/nprot", "Nature Protocols"),
    ("/s41597", "Scientific Data"),
    ("/s41598", "Scientific Reports"),
    ("/s41698", "npj Precision Oncology"),
    ("/s41419", "Cell Death & Disease"),
    ("/s42003", "Communications Biology"),
    ("/s44160", "Nature Synthesis"),
    ("/s43587", "npj Aging"),
    ("/npjaging", "npj Aging"),
    ("/s41514", "npj Aging"),
    ("/s43588", "Nature Computational Science"),
    ("/s41540", "npj Systems Biology and Applications"),
    ("/s41601", "npj Biofilms and Microbiomes"),
    ("/s41684", "Lab Animal"),
    ("/s41551", "Nature Biomedical Engineering"),
    ("/s41563", "Nature Materials"),
    ("/s41564", "Nature Microbiology"),
    ("/s41566", "Nature Photonics"),
    ("/s41612", "npj Digital Medicine"),
    ("/s41746", "npj Digital Medicine"),
    ("/s41392", "Signal Transduction and Targeted Therapy"),
    ("/s41565", "Nature Nanotechnology"),
]

# ScienceDirect path map (extends Cell with PII-based detection)
_SD_PATH_MAP: list[tuple[str, str]] = _CELL_PATH_MAP + [
    ("s00928674", "Cell"),
    ("s10747613", "Immunity"),
]

# Domains that use path-based matching (domain, path_map, default_name)
_PATH_MATCHERS: list[tuple[str, list[tuple[str, str]], str]] = [
    ("cell.com", _CELL_PATH_MAP, "Cell Press"),
    ("sciencedirect.com", _SD_PATH_MAP, "Cell Press"),
    ("science.org", _SCIENCE_PATH_MAP, "Science"),
    ("nature.com", _NATURE_PATH_MAP, "Nature Group"),
    ("nature-com", _NATURE_PATH_MAP, "Nature Group"),
]


def _match_path(path: str, mapping: list[tuple[str, str]]) -> str | None:
    for segment, name in mapping:
        if segment in path:
            return name
    return None


def _domain_journal(url: str) -> str | None:
    domain = urlparse(url).netloc.lower().removeprefix("www.")
    path = urlparse(url).path.lower()

    for skip in _NON_JOURNAL_DOMAINS:
        if skip in domain:
            return None

    for key, name in _SIMPLE_DOMAIN_MAP.items():
        if key in domain:
            return name

    for match_domain, path_map, default in _PATH_MATCHERS:
        if match_domain in domain:
            result = _match_path(path, path_map)
            if result:
                return result
            # Special case: Trends in cell.com
            if "cell.com" in domain and "/trends/" in path:
                m = re.search(r"/trends/([^/]+)/", path)
                if m:
                    return f"Trends in {m.group(1).capitalize()}"
                return "Trends"
            return default

    return None


# ---------------------------------------------------------------------------
# Crossref API lookup
# ---------------------------------------------------------------------------


def _crossref_journal(doi: str, session: requests.Session) -> str | None:
    url = CROSSREF_API.format(doi=doi)
    try:
        resp = session.get(url, timeout=15)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        titles = data.get("message", {}).get("container-title", [])
        return titles[0] if titles else None
    except Exception as exc:
        log.debug("Crossref lookup failed for %s: %s", doi, exc)
        return None


# ---------------------------------------------------------------------------
# DOI prefix → journal fallback
# ---------------------------------------------------------------------------

_DOI_PREFIX_MAP: dict[str, str] = {
    "10.48550/arXiv.": "arXiv",
}


def _doi_prefix_journal(doi: str) -> str | None:
    for prefix, name in _DOI_PREFIX_MAP.items():
        if doi.startswith(prefix):
            return name
    return None


# ---------------------------------------------------------------------------
# DOI redirect follower (last resort)
# ---------------------------------------------------------------------------

_DOI_REDIRECT_MAP: list[tuple[callable, callable]] = [
    (lambda d: "biorxiv.org" in d, lambda _: "bioRxiv"),
    (lambda d: "medrxiv.org" in d, lambda _: "medRxiv"),
    (lambda d: "cell.com" in d or "sciencedirect.com" in d,
     lambda d: _domain_journal(f"https://{d}") or "Cell Press"),
    (lambda d: "nature.com" in d,
     lambda d: _domain_journal(f"https://{d}") or "Nature Group"),
    (lambda d: "science.org" in d, lambda _: "Science"),
    (lambda d: "pnas.org" in d, lambda _: "PNAS"),
]


def _resolve_doi_domain(doi: str, session: requests.Session) -> str | None:
    try:
        resp = session.get(
            f"https://doi.org/{doi}",
            timeout=15,
            allow_redirects=True,
        )
        final = resp.url.lower()
        domain = urlparse(final).netloc.removeprefix("www.")
        for condition, name_fn in _DOI_REDIRECT_MAP:
            if condition(domain):
                return name_fn(domain)
        j = _domain_journal(final)
        if j:
            return j
        domain_name = domain.split(".")[0]
        return domain_name.capitalize() if domain_name else None
    except Exception as exc:
        log.debug("DOI redirect failed for %s: %s", doi, exc)
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    if not CSV_PATH.exists():
        log.error("papers.csv not found at %s", CSV_PATH)
        return

    if requests is None:
        log.error("requests library not available")
        return

    with CSV_PATH.open() as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    fieldnames = reader.fieldnames or []
    has_doi_col = "doi" in fieldnames
    has_journal_col = "journal" in fieldnames
    if not has_doi_col:
        fieldnames.append("doi")
    if not has_journal_col:
        fieldnames.append("journal")

    session = requests.Session()
    session.headers["User-Agent"] = (
        "RendeiroLabCoolPapers/1.0 (mailto:arendeiro@cemm.oeaw.ac.at)"
    )

    total = len(rows)
    doi_found = 0
    journal_crossref = 0
    journal_domain = 0
    no_journal = 0

    for i, row in enumerate(rows):
        if i and i % 50 == 0:
            log.info("Progress: %d/%d", i, total)

        url = row.get("url", "").strip()

        doi = row.get("doi", "").strip() if has_doi_col else ""
        if not doi:
            doi = extract_doi(url) or ""
            row["doi"] = doi

        if doi:
            doi_found += 1

        existing_journal = row.get("journal", "").strip() if has_journal_col else ""
        if existing_journal:
            continue

        journal = ""

        if doi:
            journal = _crossref_journal(doi, session) or ""
            if journal:
                journal_crossref += 1
            time.sleep(0.05)

        if not journal and url.startswith("http"):
            journal = _domain_journal(url) or ""
            if journal:
                journal_domain += 1

        if not journal and doi:
            journal = _doi_prefix_journal(doi) or ""
            if journal:
                journal_domain += 1

        if not journal and doi and url.startswith("https://doi.org/"):
            journal = _resolve_doi_domain(doi, session) or ""
            if journal:
                journal_domain += 1

        row["journal"] = journal
        if not journal:
            no_journal += 1

    with CSV_PATH.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    log.info("Done — %d papers processed", total)
    log.info("DOIs extracted: %d", doi_found)
    log.info("Journal from Crossref: %d", journal_crossref)
    log.info("Journal from domain  : %d", journal_domain)
    log.info("No journal found     : %d", no_journal)
    log.info("Wrote %s", CSV_PATH)


if __name__ == "__main__":
    main()
