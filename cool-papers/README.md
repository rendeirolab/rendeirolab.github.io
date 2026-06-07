# Cool Papers

Extracts `[paper]`-tagged emails from a Thunderbird mbox file, enriches them with DOI/journal metadata, and renders a year-tabbed HTMX page at `/cool-papers/`.

## Pipeline

1. **Extract** — `extract_papers.py` (uv script)
   - Greps the mbox (12GB, 155M lines, 22K messages) for message-boundary offsets and `[paper]` subjects (554 matches)
   - Uses `mmap` + `bisect_right` to extract only matching messages
   - Parses multipart/single-part MIME, QP/base64, extracts title/URL/comment/date/sender
   - Strips HTML, signatures, style/script tags, Outlook safelinks, `^`/`^^` markers

2. **Enrich** — `enrich_papers.py` (uv script)
   - Extracts DOIs from URLs (doi.org, nature.com, science.org, Cell.com, etc.)
   - Queries Crossref API for journal names (272 matches)
   - Falls back to domain mapping (bioRxiv, medRxiv, arXiv, 30+ Nature journals, Cell Press, etc.) (264 matches)
   - Last resort: follows DOI redirect to determine publisher (18 non-publisher entries remain without journal)

3. **Build** — `build.py` (project root)
   - Reads `papers.csv` + `content/team.yaml` for sender→name mapping
   - Groups by year, generates `index.html` (year tabs) + `year-YYYY.html` fragments

## Files

| File | Purpose |
|------|---------|
| `extract_papers.py` | mbox → CSV extraction (run once) |
| `enrich_papers.py` | add DOI/journal columns (run once after extraction) |
| `papers.csv` | enriched output, 554 rows, 10 columns |
| `from_offsets.txt` | grep index of message boundaries |
| `paper_lines.txt` | grep index of `[paper]` subjects |

## Dependencies

- `extract_papers.py` — `beautifulsoup4`
- `enrich_papers.py` — `requests`

Both use `uv run` with inline dependency declarations.

## Usage

```bash
# Clone, then from project root:
uv run cool-papers/extract_papers.py
uv run cool-papers/enrich_papers.py
uv run build.py
```

## Output

- `docs/cool-papers/index.html` — 72K main page (5 year tabs)
- `docs/cool-papers/year-YYYY.html` — per-year fragment (12K–124K each)
