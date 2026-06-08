#!/usr/bin/env -S uv run
# /// script
# dependencies = [
#   "pandas",
#   "numpy",
#   "scikit-learn",
#   "sentence-transformers",
#   "umap-learn",
#   "matplotlib",
#   "scipy",
#   "pyyaml",
# ]
# ///

"""News insights: topic clustering and trends from news YAML.

Usage:
    uv run cool-papers/news_insights.py

Output:
    cool-papers/news_insights/*.svg, *.csv
"""

import hashlib
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("news_insights")

SCRIPT_DIR = Path(__file__).parent.resolve()
ROOT_DIR = SCRIPT_DIR.parent
NEWS_YAML = ROOT_DIR / "content" / "news.yaml"
OUT_DIR = SCRIPT_DIR / "news_insights"
OUT_DIR.mkdir(exist_ok=True)

N_CLUSTERS = 6

# Team member first names to filter from topic labels
_TEAM_NAMES = [
    "andre", "ariadna", "clemens", "ernesto", "iva", "lisa",
    "parijat", "samir", "simon", "shrestha", "tamas", "yimin",
]


# ── Data loading ───────────────────────────────────────────────────────


def load_news() -> pd.DataFrame:
    with open(NEWS_YAML) as f:
        data = yaml.safe_load(f)
    items = data["news"]["news"]
    rows = []
    for item in items:
        rows.append({
            "title": item["title"],
            "date": str(item["date"]),
            "description": item.get("description", ""),
        })
    df = pd.DataFrame(rows)
    df["date_parsed"] = pd.to_datetime(df["date"], utc=True)
    log.info("Loaded %d news items", len(df))
    return df


# ── Embeddings ─────────────────────────────────────────────────────────


def embed_titles(titles: list[str]) -> np.ndarray:
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("all-MiniLM-L6-v2")
    return model.encode(titles, show_progress_bar=True)


def _cache_key(titles: list[str]) -> bytes:
    return hashlib.md5("".join(titles).encode()).digest()


def _load_or_embed(titles: list[str]) -> np.ndarray:
    emb_path = OUT_DIR / "embeddings.npy"
    key_path = OUT_DIR / "embeddings_key.txt"

    if emb_path.exists() and key_path.exists():
        cached_key = key_path.read_text().strip()
        current_key = _cache_key(titles).hex()
        if cached_key == current_key:
            log.info("Loading cached embeddings from %s", emb_path)
            return np.load(emb_path)
        log.info("Titles changed, re-embedding …")

    log.info("Embedding %d titles …", len(titles))
    emb = embed_titles(titles)
    np.save(emb_path, emb)
    key_path.write_text(_cache_key(titles).hex())
    log.info("Cached embeddings to %s", emb_path)
    return emb


# ── Clustering ─────────────────────────────────────────────────────────


def cluster_and_assign(emb: np.ndarray) -> np.ndarray:
    from sklearn.cluster import KMeans
    from sklearn.preprocessing import normalize

    emb_norm = normalize(emb, norm="l2")
    km = KMeans(n_clusters=N_CLUSTERS, random_state=42, n_init="auto")
    labels = km.fit_predict(emb_norm)
    n_clusters = len(set(labels))
    log.info("KMeans: %d clusters, all %d items assigned", n_clusters, len(emb))
    return labels


# ── Topic labeling ─────────────────────────────────────────────────────


def _tfidf_labels(titles: pd.Series, labels: np.ndarray) -> dict:
    from sklearn.feature_extraction.text import TfidfVectorizer, ENGLISH_STOP_WORDS

    stop_words = list(ENGLISH_STOP_WORDS) + _TEAM_NAMES

    vectorizer = TfidfVectorizer(
        stop_words=stop_words,
        max_features=1000,
        ngram_range=(1, 2),
        max_df=0.85,
        min_df=2,
    )
    tfidf = vectorizer.fit_transform(titles)
    feature_names = vectorizer.get_feature_names_out()

    topic_labels = {}
    for tid in sorted(set(labels)):
        mask = labels == tid
        centroid = tfidf[mask].mean(axis=0).A1
        top_idx = centroid.argsort()[-3:][::-1]
        top_terms = [feature_names[i] for i in top_idx if centroid[i] > 0]
        if top_terms:
            topic_labels[int(tid)] = ", ".join(top_terms)
        else:
            topic_labels[int(tid)] = f"Topic {tid}"

    log.info("Topic labels: %s", topic_labels)
    return topic_labels


# ── UMAP reduction ─────────────────────────────────────────────────────


def reduce_umap(emb: np.ndarray) -> np.ndarray:
    import umap

    log.info("Reducing to 2D with UMAP …")
    reducer = umap.UMAP(n_neighbors=20, min_dist=0.1, random_state=42)
    emb_2d = reducer.fit_transform(emb)
    log.info("UMAP done")
    return emb_2d


# ── Time trends ────────────────────────────────────────────────────────


def compute_trends(df: pd.DataFrame, labels: np.ndarray) -> pd.DataFrame:
    df = df.copy()
    df["topic"] = labels
    df["year_month"] = df["date_parsed"].dt.strftime("%Y-%m")

    min_date = df["date_parsed"].min()
    max_date = df["date_parsed"].max()
    all_months = pd.date_range(start=min_date, end=max_date, freq="MS", tz="UTC")
    month_grid = pd.DataFrame({
        "year_month": all_months.strftime("%Y-%m"),
        "date": all_months,
    })

    def _fill(series, grid):
        merged = grid.merge(series, on="year_month", how="left")
        merged["count"] = merged["count"].fillna(0).astype(int)
        merged["date"] = pd.to_datetime(merged["date"], utc=True)
        return merged

    # Total per month
    total = df.groupby("year_month").size().reset_index(name="count")
    total = _fill(total, month_grid)
    total["topic"] = -1

    # Per topic per month
    per_topic = df.groupby(["year_month", "topic"]).size().reset_index(name="count")
    filled = []
    for t in sorted(per_topic["topic"].unique()):
        t_series = per_topic[per_topic["topic"] == t][["year_month", "count"]]
        t_filled = _fill(t_series, month_grid)
        t_filled["topic"] = t
        filled.append(t_filled)
    per_topic = pd.concat(filled, ignore_index=True)

    combined = pd.concat([total, per_topic], ignore_index=True)
    combined = combined.sort_values(["topic", "date"])
    combined["smoothed"] = combined.groupby("topic")["count"].transform(
        lambda x: x.rolling(3, min_periods=1).mean()
    )
    return combined


# ── Growth ─────────────────────────────────────────────────────────────


def compute_growth(df: pd.DataFrame, labels: np.ndarray) -> pd.DataFrame:
    from scipy import stats as sp_stats

    trends = compute_trends(df, labels)
    last_date = df["date_parsed"].max()
    three_months_ago = last_date - pd.Timedelta(days=90)

    _MIN_MEAN = 0.1

    rows = []
    for topic in sorted(set(labels)):
        if topic == -1:
            continue
        t = trends[trends["topic"] == topic].copy()
        if len(t) < 4:
            continue

        mean_count = float(t["smoothed"].mean())
        if mean_count < _MIN_MEAN:
            continue

        t["x"] = np.arange(len(t))
        slope, _, _, pval, _ = sp_stats.linregress(t["x"], t["smoothed"])
        growth_rate = slope / mean_count

        t3 = t[t["date"] >= three_months_ago]
        if len(t3) >= 3:
            t3["x"] = np.arange(len(t3))
            slope_3mo, _, _, pval_3mo, _ = sp_stats.linregress(t3["x"], t3["smoothed"])
            mean_3mo = float(t3["smoothed"].mean())
            growth_rate_3mo = slope_3mo / mean_3mo if mean_3mo > 0 else 0.0
        else:
            growth_rate_3mo = growth_rate
            pval_3mo = pval

        rate_overall = mean_count
        rate_recent = float(t3["smoothed"].mean()) if len(t3) >= 3 else mean_count
        surge_ratio = rate_recent / rate_overall if rate_overall > 0 else 1.0

        total_count = int(t["count"].sum())
        rows.append({
            "topic": topic,
            "total_papers": total_count,
            "growth_rate": round(growth_rate, 4),
            "growth_rate_3mo": round(growth_rate_3mo, 4),
            "surge_ratio": round(surge_ratio, 4),
            "p_value": round(pval, 6),
            "p_value_3mo": round(pval_3mo, 6),
        })

    growth_df = pd.DataFrame(rows)

    sig = growth_df[growth_df["p_value"] < 0.1]
    if len(sig) >= 3:
        growth_df = sig.copy()

    growth_df["hot"] = growth_df["growth_rate_3mo"] > 0.10
    growth_df["cold"] = growth_df["growth_rate_3mo"] < -0.10
    return growth_df


# ── Plotting ───────────────────────────────────────────────────────────


_THEMES = {
    "dark": {
        "figure.facecolor": "none",
        "axes.facecolor": "none",
        "axes.edgecolor": "#ffffff",
        "axes.labelcolor": "#ffffff",
        "axes.titlecolor": "#ffffff",
        "text.color": "#ffffff",
        "xtick.color": "#ffffff",
        "ytick.color": "#ffffff",
        "grid.color": "#666688",
        "grid.alpha": 0.3,
        "legend.facecolor": "#1a1a2e",
        "legend.edgecolor": "#666688",
        "legend.labelcolor": "#ffffff",
        "font.size": 11,
    },
    "light": {
        "figure.facecolor": "none",
        "axes.facecolor": "none",
        "axes.edgecolor": "#000000",
        "axes.labelcolor": "#000000",
        "axes.titlecolor": "#000000",
        "text.color": "#000000",
        "xtick.color": "#000000",
        "ytick.color": "#000000",
        "grid.color": "#cccccc",
        "grid.alpha": 0.5,
        "legend.facecolor": "#ffffff",
        "legend.edgecolor": "#cccccc",
        "legend.labelcolor": "#000000",
        "font.size": 11,
    },
}


def _setup_style(theme: str):
    import matplotlib
    matplotlib.use("SVG")
    import matplotlib.pyplot as plt

    plt.rcParams.update(_THEMES[theme])
    return plt


def _despine(fig):
    for ax in fig.axes:
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)


def _plot_and_save(fig, out: Path, theme: str):
    """Remove top/right spines, then save with theme suffix."""
    import matplotlib.pyplot as plt
    _despine(fig)
    stem = out.stem
    suffix = "" if theme == "dark" else f"-{theme}"
    themed_path = out.with_name(f"{stem}{suffix}.svg")
    fig.savefig(themed_path, format="svg", bbox_inches="tight", pad_inches=0, transparent=True)
    plt.close(fig)
    log.debug("Saved %s", themed_path.name)


def _color_palette(n: int) -> list:
    import matplotlib.cm as cm
    import matplotlib.colors as mcolors
    import numpy as np

    base = cm.tab20(np.linspace(0, 1, 20))
    if n <= 20:
        return [mcolors.to_hex(c) for c in base[:n]]
    extra = cm.tab20b(np.linspace(0, 1, n - 20))
    return [mcolors.to_hex(c) for c in list(base) + list(extra)]


def plot_umap(emb_2d: np.ndarray, labels: np.ndarray, topic_labels: dict, out: Path, theme: str):
    plt = _setup_style(theme)
    fig, ax = plt.subplots(figsize=(10, 7))
    colors = _color_palette(len(set(labels)))

    for tid in sorted(set(labels)):
        mask = labels == tid
        ax.scatter(
            emb_2d[mask, 0], emb_2d[mask, 1],
            c=colors[tid], label=topic_labels.get(int(tid), f"Topic {tid}"),
            alpha=0.7, s=30, edgecolors="none",
        )
        centroid = emb_2d[mask].mean(axis=0)
        ax.scatter(centroid[0], centroid[1], c=colors[tid], marker="^",
                   s=80, edgecolors="none", zorder=5)

    ax.set_title("News topic map", fontsize=14)
    ax.set_xlabel("UMAP 1")
    ax.set_ylabel("UMAP 2")
    legend = ax.legend(loc="best", fontsize=7, framealpha=0.7)
    for lh in legend.legend_handles:
        lh._sizes = [40]
    fig.tight_layout()
    _plot_and_save(fig, out, theme)


def plot_trends_overall(df: pd.DataFrame, out: Path, theme: str):
    plt = _setup_style(theme)
    fig, ax = plt.subplots(figsize=(10, 4))

    monthly = df["date_parsed"].dt.strftime("%Y-%m").value_counts().reset_index()
    monthly.columns = ["year_month", "count"]
    min_date = df["date_parsed"].min()
    max_date = df["date_parsed"].max()
    all_months = pd.date_range(start=min_date, end=max_date, freq="MS", tz="UTC")
    month_grid = pd.DataFrame({
        "year_month": all_months.strftime("%Y-%m"),
        "date": all_months,
    })
    monthly = month_grid.merge(monthly, on="year_month", how="left")
    monthly["count"] = monthly["count"].fillna(0).astype(int)
    monthly["date"] = pd.to_datetime(monthly["date"], utc=True)
    monthly["smoothed"] = monthly["count"].rolling(3, min_periods=1).mean()

    bar_color = "#4361ee" if theme == "dark" else "#4361ee"
    line_color = "#f72585" if theme == "dark" else "#d90429"

    ax.bar(monthly["date"], monthly["count"], width=20, color=bar_color, alpha=0.45, label="Items per month")
    ax.plot(monthly["date"], monthly["smoothed"], color=line_color, linewidth=2, label="3-month average")

    ax.set_title("News items per month", fontsize=14)
    ax.set_xlabel("Date")
    ax.set_ylabel("Items")
    ax.legend(loc="upper left")
    fig.tight_layout()
    _plot_and_save(fig, out, theme)


def plot_growth(growth_df: pd.DataFrame, topic_labels: dict, out: Path, theme: str):
    plt = _setup_style(theme)
    sorted_df = growth_df.sort_values("growth_rate", ascending=False)

    fig, ax = plt.subplots(figsize=(8, 4))
    labels_display = [topic_labels.get(int(t), f"Topic {t}") for t in sorted_df["topic"]]
    stable_color = "#999999"
    hot_color = "#f72585"
    cold_color = "#4cc9f0"

    bars = ax.barh(range(len(sorted_df)), sorted_df["growth_rate"] * 100, color=stable_color, alpha=0.6)
    n_hot = n_cold = 0
    for i, (_, row) in enumerate(sorted_df.iterrows()):
        if row["hot"]:
            bars[i].set_color(hot_color)
            bars[i].set_alpha(0.85)
            n_hot += 1
        elif row["cold"]:
            bars[i].set_color(cold_color)
            bars[i].set_alpha(0.85)
            n_cold += 1

    ax.set_yticks(range(len(sorted_df)))
    ax.set_yticklabels(labels_display, fontsize=9)
    ax.set_xlabel("Relative growth rate (% per month)")
    ax.set_title("Topic growth over time", fontsize=14)
    ax.axvline(0, color="#666666", linewidth=0.8)
    x_min, x_max = ax.get_xlim()
    pad = max(abs(x_max - x_min) * 0.15, 3)
    ax.set_xlim(x_min - pad, x_max + pad)

    from matplotlib.patches import Patch
    legend_elements = []
    if n_hot > 0:
        legend_elements.append(Patch(facecolor=hot_color, alpha=0.85, label="Hot (>10%/mo)"))
    if n_cold > 0:
        legend_elements.append(Patch(facecolor=cold_color, alpha=0.85, label="Cold (<-10%/mo)"))
    legend_elements.append(Patch(facecolor=stable_color, alpha=0.6, label="Stable"))
    if legend_elements:
        ax.legend(handles=legend_elements, loc="lower right")
    fig.tight_layout()
    _plot_and_save(fig, out, theme)


# ── Main ───────────────────────────────────────────────────────────────


def main():
    OUT_DIR.mkdir(exist_ok=True)

    df = load_news()
    titles = df["title"].tolist()

    log.info("Step 1/5: Embedding …")
    emb = _load_or_embed(titles)

    log.info("Step 2/5: Clustering …")
    labels = cluster_and_assign(emb)

    log.info("Step 3/5: Labeling topics …")
    topic_labels = _tfidf_labels(df["title"], labels)
    log.info("Topic labels: %s", topic_labels)

    log.info("Step 4/5: Reducing to 2D …")
    emb_2d = reduce_umap(emb)

    log.info("Step 5/5: Computing trends & growth + plotting …")
    trends = compute_trends(df, labels)

    for theme in ("dark", "light"):
        plot_umap(emb_2d, labels, topic_labels, OUT_DIR / "umap.svg", theme)
        plot_trends_overall(df, OUT_DIR / "trends_overall.svg", theme)

    # Write CSVs
    out_items = df[["title", "date", "date_parsed"]].copy()
    out_items["topic"] = labels
    out_items["topic_label"] = out_items["topic"].map(topic_labels).fillna("Other")
    out_items["umap_x"] = emb_2d[:, 0]
    out_items["umap_y"] = emb_2d[:, 1]
    out_items.to_csv(OUT_DIR / "news_topics.csv", index=False)
    log.info("Wrote %s", OUT_DIR / "news_topics.csv")

    trends.to_csv(OUT_DIR / "news_trends.csv", index=False)
    log.info("Wrote %s", OUT_DIR / "news_trends.csv")

    log.info("Done!")


if __name__ == "__main__":
    main()
