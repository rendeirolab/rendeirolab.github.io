# Cool Papers

Extracts `[paper]`-tagged emails from the local **moe** mail store (SQLite), enriches them with DOI/journal metadata, and renders a year-tabbed HTMX page at `/cool-papers/` with a topic-insights panel.

## Pipeline

1. **Extract** — `extract_papers.py` (uv script)
   - Reads `[paper]`-tagged messages from moe's local SQLite store (default `~/.local/share/moe/moe.db`), scanning both the INBOX and Sent Items folders; configured via the `cool_papers:` section of the root `config.yaml` (`moe_db`, `inbox_folder`, `sent_folders`) or the `MOE_DB`/`MOE_INBOX`/`MOE_SENT_FOLDERS` env vars.
   - Falls back to the legacy Thunderbird mbox scanner (`mailbox`/`sent_mailbox` config keys, `MAILBOX`/`SENT_MAILBOX` env vars) when no moe database is configured or the file is missing.
   - **mbox archive as reference:** when the Thunderbird mbox files are still on disk, they are also scanned in moe mode. They hold the raw MIME messages up to their last sync (hyperlinks intact), so they are preferred over moe's cached plain-text bodies for the papers they contain. RFC 2047-encoded `[paper]` subjects (non-ASCII titles) are indexed too.
   - Uses the cached searchable body text (`message_fts.body`), and the raw `rfc822` blob only when its Subject header matches the row. moe can associate the wrong message's blob/body with an inbox row: such a body is only trusted when it contains the paper title; otherwise the full-text index is searched for a body that starts with the title (the right body is often attached to another row).
   - **URL:** candidates are taken in document order (Outlook `originalsrc`/`href` attributes and plain-text URLs, safelinks unwrapped) after cutting the signature and any quoted reply. The highest-priority publisher/archive URL wins (first on ties); code/data hosts (GitHub, Zenodo, Hugging Face, ...) and other non-junk URLs are only used when there is none. Parentheses in URLs are kept (Cell/Lancet `S0092-8674(25)00749-4`-style PIIs), while trailing punctuation and unbalanced brackets are trimmed.
   - **Comment:** the message text without greetings, the title (also when wrapped or followed by a site suffix), the paper URL, Outlook link-preview cards, image placeholders, sign-offs/signatures and quoted replies; other URLs (related papers, code, data) are kept. Bodies are decoded with their declared charset.
   - **Title:** browser-tab site suffixes pasted with the title (`...: Cell`, `... - The Lancet Digital Health`, `... - ScienceDirect`) are stripped.
   - **Duplicates:** a paper shared several times keeps its best occurrence (with a URL, from a verified body, with a comment, from the mbox, sent over inbox), the latest one on ties.
   - **Carry-forward:** enrichment, URLs, and comments from the previous `papers.csv` are merged back by title; when a body could not be verified to belong to its message, the previous run's URL/comment win; papers that vanished from the mail store are restored instead of dropped.
   - **Staleness guard:** refuses to run (exit code 1) when the moe INBOX folder has not synced in the last 7 days (`sync_state.last_sync_at`) — or, in mbox mode, when no `[paper]`-tagged email was found in the last 7 days. Override with `ALLOW_STALE_MAIL=1`

2. **Enrich** — `enrich_papers.py` (uv script)
   - Extracts DOIs from URLs (doi.org, nature.com, science.org, Cell.com, etc.)
   - Queries Crossref API for journal names (272 matches)
   - Falls back to domain mapping (bioRxiv, medRxiv, arXiv, 30+ Nature journals, Cell Press, etc.) (264 matches)
   - Last resort: follows DOI redirect to determine publisher (18 non-publisher entries remain without journal)

3. **Insights** — `insights.py` (uv script, optional)
   - Embeds paper titles with `all-MiniLM-L6-v2` (384-dim, cached to `embeddings.npy`)
   - Clusters into 20 topics via KMeans (cosine distance on L2-normalized embeddings)
   - Labels each topic with top TF-IDF bigrams
   - Reduces to 2D with UMAP for the cluster scatter plot
   - Computes weekly trends, per-topic growth (linear regression), hot-topic detection
   - Generates 7 themed SVGs (dark + light variants each) to the `insights/` directory

4. **Build** — `build.py` (project root)
   - Reads `papers.csv` + `content/team.yaml` for sender→name mapping
   - Groups by year, generates `index.html` (year tabs + Insights button) + `year-YYYY.html` fragments
   - Reads `insights/` data and renders the insights HTMX fragment at `insights.html`
   - Copies SVGs and CSVs to the build output

## Files

| File | Purpose |
|------|---------|
| `extract_papers.py` | moe SQLite (or mbox fallback) → CSV extraction (run once) |
| `enrich_papers.py` | add DOI/journal columns (run once after extraction) |
| `insights.py` | topic clustering, trends, plots (re-runnable with cache) |
| `papers.csv` | enriched output, 789 rows, 10 columns |
| `from_offsets_*.txt` | grep index of message boundaries (mbox mode only) |
| `paper_lines_*.txt` | grep index of `[paper]` subjects (mbox mode only) |
| `insights/` | generated SVGs, CSVs, and embedding cache |

### `insights/` contents

| File | Contents |
|------|----------|
| `umap.svg` / `umap-light.svg` | UMAP scatter plot with cluster centroids |
| `trends_overall.svg` | papers per week (bar + 4-week average) |
| `trends_per_topic.svg` | per-topic weekly trends (top 10) |
| `growth.svg` | topic growth coefficients (barh) |
| `hot_topics.svg` | hottest topics last 3 months |
| `weekday.svg` | papers shared per weekday |
| `hour.svg` | papers shared per hour of day (UTC) |
| `paper_topics.csv` | per-paper topic assignment + UMAP coords |
| `topic_trends.csv` | weekly counts per topic |
| `topic_growth.csv` | growth slopes per topic |
| `embeddings.npy` | cached 384-dim title embeddings |
| `embeddings_key.txt` | MD5 hash of titles for cache invalidation |

## Dependencies

- `extract_papers.py` — `beautifulsoup4`
- `enrich_papers.py` — `requests`
- `insights.py` — `sentence-transformers`, `umap-learn`, `scikit-learn`, `matplotlib`, `pandas`, `numpy`

All use `uv run` with inline dependency declarations.

## Usage

```bash
# Clone, then from project root:

# Step 1 — extract papers from moe (or the mbox fallback; run once)
uv run cool-papers/extract_papers.py

# Step 2 — enrich with DOI/journal (run once)
uv run cool-papers/enrich_papers.py

# Step 3 — generate insights (re-runnable; embeddings cached)
uv run cool-papers/insights.py

# Step 4 — build the site
uv run build.py
```

## Output

- `docs/cool-papers/index.html` — main page (year tabs + Insights button)
- `docs/cool-papers/year-YYYY.html` — per-year HTMX fragments
- `docs/cool-papers/insights.html` — insights fragment (loaded by Insights tab)
- `docs/cool-papers/insights/` — SVGs (dark + light) and CSVs
- `docs/cool-papers/feed.xml` — RSS feed (100 items)

## Plot themes

SVGs are generated in two variants: `name.svg` (dark theme, white text) and `name-light.svg` (light theme, black text). The `_cool_papers_insights.html` fragment toggles visibility via CSS based on the page's `data-bs-theme` attribute (dark, light, or auto).
