#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "sentence-transformers",
#   "umap-learn",
#   "scikit-learn",
#   "matplotlib",
#   "pandas",
#   "numpy",
# ]
# ///

"""Generate topic insights for cool papers: embeddings, clustering, trends, plots.

Output written to cool-papers/insights/:
  paper_topics.csv   — per-paper topic assignments + UMAP coords
  topic_trends.csv   — weekly paper counts per topic (smoothed)
  topic_growth.csv   — growth coefficients per topic
  *.svg              — figures

Usage:
    uv run cool-papers/insights.py
"""

import csv
import logging
import re
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("insights")

SCRIPT_DIR = Path(__file__).parent.resolve()
CSV_PATH = SCRIPT_DIR / "papers.csv"
OUT_DIR = SCRIPT_DIR / "insights"
OUT_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# 1. Load data
# ---------------------------------------------------------------------------

def load_papers() -> pd.DataFrame:
    df = pd.read_csv(CSV_PATH)
    df["date_parsed"] = pd.to_datetime(df["date_parsed"], utc=True)
    df = df.dropna(subset=["title"])
    df["title"] = df["title"].str.strip()
    df = df[df["title"] != ""]
    log.info("Loaded %d papers", len(df))
    return df


# ---------------------------------------------------------------------------
# 2. Embed titles
# ---------------------------------------------------------------------------

def embed_titles(titles: list[str]) -> np.ndarray:
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer("all-MiniLM-L6-v2")
    log.info("Embedding %d titles …", len(titles))
    emb = model.encode(titles, show_progress_bar=True)
    log.info("Embeddings shape: %s", emb.shape)
    return emb


# ---------------------------------------------------------------------------
# 3. Cluster embeddings
# ---------------------------------------------------------------------------

def cluster_and_assign(emb: np.ndarray, n_clusters: int = 20) -> np.ndarray:
    """Cluster embeddings with KMeans (cosine distance on L2-normalized vectors)."""
    from sklearn.cluster import KMeans
    from sklearn.preprocessing import normalize

    log.info("Clustering with KMeans (n_clusters=%d) …", n_clusters)
    emb_norm = normalize(emb)
    clusterer = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
    labels = clusterer.fit_predict(emb_norm)

    log.info("KMeans: %d clusters, all %d papers assigned", n_clusters, len(labels))
    return labels


# ---------------------------------------------------------------------------
# 4. Label clusters via TF-IDF
# ---------------------------------------------------------------------------

def _tfidf_labels(titles: pd.Series, labels: np.ndarray) -> dict[int, str]:
    from sklearn.feature_extraction.text import TfidfVectorizer
    import numpy as np

    # Clean titles: lowercase, simple tokenization
    clean = titles.str.lower().str.replace(r"[^a-z0-9\s]", " ", regex=True)
    clean = clean.str.replace(r"\s+", " ", regex=True).str.strip()

    vectorizer = TfidfVectorizer(
        stop_words="english",
        max_features=500,
        ngram_range=(1, 2),
    )
    tfidf = vectorizer.fit_transform(clean)

    labels_map: dict[int, str] = {}
    for label in sorted(set(labels)):
        if label == -1:
            continue
        mask = labels == label
        centroid = tfidf[mask].mean(axis=0).A1
        top_idx = np.argsort(centroid)[-5:][::-1]
        terms = [vectorizer.get_feature_names_out()[i] for i in top_idx]
        # Capitalize first letter
        label_str = ", ".join(t.capitalize() for t in terms[:3])
        labels_map[label] = label_str

    return labels_map


# ---------------------------------------------------------------------------
# 5. UMAP reduction
# ---------------------------------------------------------------------------

def reduce_umap(emb: np.ndarray) -> np.ndarray:
    import umap
    log.info("Reducing to 2D with UMAP …")
    reducer = umap.UMAP(random_state=42, min_dist=0.3, n_neighbors=15)
    emb_2d = reducer.fit_transform(emb)
    log.info("UMAP done")
    return emb_2d


# ---------------------------------------------------------------------------
# 6. Time trends
# ---------------------------------------------------------------------------

def compute_trends(df: pd.DataFrame, labels: np.ndarray) -> pd.DataFrame:
    """Weekly paper counts per topic, with 4-week rolling average."""
    df = df.copy()
    df["topic"] = labels
    df["week"] = df["date_parsed"].dt.isocalendar().week.astype(int)
    df["year"] = df["date_parsed"].dt.year
    # ISO week year can differ from calendar year for first/last weeks
    df["year_week"] = df["date_parsed"].dt.strftime("%Y-%W")

    # Total counts per week
    total = df.groupby("year_week").size().reset_index(name="count")
    total["topic"] = -1  # sentinel for total
    total["date"] = pd.to_datetime(total["year_week"] + "-1", format="%Y-%W-%w", utc=True)

    # Per topic per week
    per_topic = df.groupby(["year_week", "topic"]).size().reset_index(name="count")
    per_topic["date"] = pd.to_datetime(per_topic["year_week"] + "-1", format="%Y-%W-%w", utc=True)

    combined = pd.concat([total, per_topic], ignore_index=True)
    combined = combined.sort_values(["topic", "date"])
    # 4-week rolling average
    combined["smoothed"] = combined.groupby("topic")["count"].transform(
        lambda x: x.rolling(4, min_periods=1).mean()
    )
    return combined


def compute_growth(df: pd.DataFrame, labels: np.ndarray) -> pd.DataFrame:
    """Linear regression slope on weekly counts per topic."""
    from scipy import stats as sp_stats

    trends = compute_trends(df, labels)
    last_date = df["date_parsed"].max()
    three_months_ago = last_date - pd.Timedelta(days=90)

    rows = []
    for topic in sorted(set(labels)):
        if topic == -1:
            continue
        t = trends[trends["topic"] == topic].copy()
        if len(t) < 4:
            continue
        t["x"] = np.arange(len(t))
        slope, _, _, pval, _ = sp_stats.linregress(t["x"], t["smoothed"])
        t3 = t[t["date"] >= three_months_ago]
        if len(t3) >= 3:
            t3["x"] = np.arange(len(t3))
            slope_3mo, _, _, _, _ = sp_stats.linregress(t3["x"], t3["smoothed"])
        else:
            slope_3mo = slope
        total_count = int(t["count"].sum())
        rows.append({
            "topic": topic,
            "total_papers": total_count,
            "growth_slope": round(slope, 4),
            "growth_3mo": round(slope_3mo, 4),
            "p_value": round(pval, 6),
        })

    growth_df = pd.DataFrame(rows)
    # Flag hot topics: top 20% by 3-month growth AND positive
    # With few topics, flag at least the best one if positive
    if len(growth_df) >= 5:
        threshold = growth_df["growth_3mo"].quantile(0.8)
    elif len(growth_df) >= 1:
        threshold = growth_df["growth_3mo"].max()
    else:
        threshold = float("inf")
    growth_df["hot"] = (growth_df["growth_3mo"] >= threshold) & (growth_df["growth_3mo"] > 0)
    return growth_df


# ---------------------------------------------------------------------------
# 7. SVGs — two themes
# ---------------------------------------------------------------------------

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


def _color_palette(n: int) -> list:
    import matplotlib.cm as cm
    import matplotlib.colors as mcolors

    base = cm.tab20(np.linspace(0, 1, 20))
    if n <= 20:
        return [mcolors.to_hex(c) for c in base[:n]]
    extra = cm.tab20b(np.linspace(0, 1, n - 20))
    return [mcolors.to_hex(c) for c in list(base) + list(extra)]


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
    log.debug("Saved %s", themed_path)


def plot_umap(emb_2d: np.ndarray, labels: np.ndarray, topic_labels: dict[int, str], out: Path, theme: str):
    plt = _setup_style(theme)
    fig, ax = plt.subplots(figsize=(10, 8))

    unique_labels = sorted(set(labels))
    colors = _color_palette(len(unique_labels))
    color_map = {lbl: colors[i] for i, lbl in enumerate(unique_labels)}
    centroid_edge = "white" if theme == "dark" else "black"

    for lbl in unique_labels:
        mask = labels == lbl
        c = color_map[lbl]
        label_str = topic_labels.get(lbl, "Noise")
        if lbl == -1:
            ax.scatter(emb_2d[mask, 0], emb_2d[mask, 1], c="#666666", s=8, alpha=0.4, label="Noise")
        else:
            ax.scatter(emb_2d[mask, 0], emb_2d[mask, 1], c=c, s=12, alpha=0.7, label=label_str)

    # Centroids as larger triangles
    for lbl in unique_labels:
        if lbl == -1:
            continue
        mask = labels == lbl
        cx, cy = emb_2d[mask].mean(axis=0)
        ax.scatter(cx, cy, marker="^", s=120, c=color_map[lbl], edgecolors=centroid_edge,
                   linewidths=0.8, zorder=5)

    ax.set_title("Paper Topics (UMAP)", fontsize=14)
    ax.set_xlabel("UMAP 1")
    ax.set_ylabel("UMAP 2")
    ax.legend(loc="best", fontsize=7, ncol=2, markerscale=1.5)
    fig.tight_layout()
    _plot_and_save(fig, out, theme)


def plot_trends_overall(trends: pd.DataFrame, out: Path, theme: str):
    plt = _setup_style(theme)
    fig, ax = plt.subplots(figsize=(12, 5))

    total = trends[trends["topic"] == -1].sort_values("date")
    bar_color = "#4361ee" if theme == "dark" else "#4361ee"
    line_color = "#f72585" if theme == "dark" else "#d90429"

    ax.bar(total["date"], total["count"], width=5, color=bar_color, alpha=0.45, label="Papers per week")
    ax.plot(total["date"], total["smoothed"], color=line_color, linewidth=2, label="4-week average")

    ax.set_title("Papers shared per week", fontsize=14)
    ax.set_xlabel("Date")
    ax.set_ylabel("Papers")
    ax.legend(loc="upper left")
    fig.tight_layout()
    _plot_and_save(fig, out, theme)


def plot_trends_per_topic(trends: pd.DataFrame, topic_labels: dict[int, str], growth_df: pd.DataFrame, out: Path, theme: str):
    plt = _setup_style(theme)
    topics = growth_df.sort_values("total_papers", ascending=False)["topic"].head(10).tolist()

    fig, axes = plt.subplots(5, 2, figsize=(14, 12), sharex=True)
    axes = axes.flatten()

    colors = _color_palette(len(topics))
    for ax_i, (topic, color) in enumerate(zip(topics, colors)):
        t = trends[(trends["topic"] == topic)].sort_values("date")
        label = topic_labels.get(topic, f"Topic {topic}")
        ax = axes[ax_i]
        ax.bar(t["date"], t["count"], width=5, color=color, alpha=0.3)
        ax.plot(t["date"], t["smoothed"], color=color, linewidth=2)
        ax.set_title(label, fontsize=10)
        ax.tick_params(axis="x", labelsize=7)
        ax.tick_params(axis="y", labelsize=7)

    for i in range(len(topics), len(axes)):
        axes[i].set_visible(False)

    fig.suptitle("Papers per week by topic (top 10)", fontsize=14)
    fig.tight_layout()
    _plot_and_save(fig, out, theme)


def plot_growth(growth_df: pd.DataFrame, topic_labels: dict[int, str], out: Path, theme: str):
    plt = _setup_style(theme)
    top = growth_df.sort_values("growth_slope", ascending=False).head(15)

    fig, ax = plt.subplots(figsize=(10, 6))
    labels_display = [topic_labels.get(t, f"Topic {t}") for t in top["topic"]]
    bar_color = "#4361ee" if theme == "dark" else "#4361ee"
    hot_color = "#f72585" if theme == "dark" else "#d90429"

    bars = ax.barh(range(len(top)), top["growth_slope"], color=bar_color, alpha=0.7)
    for i, (_, row) in enumerate(top.iterrows()):
        if row["hot"]:
            bars[i].set_color(hot_color)
            bars[i].set_alpha(0.85)

    ax.set_yticks(range(len(top)))
    ax.set_yticklabels(labels_display, fontsize=9)
    ax.set_xlabel("Growth coefficient (slope)")
    ax.set_title("Topic growth over time", fontsize=14)
    ax.axvline(0, color="#666666", linewidth=0.8)
    from matplotlib.patches import Patch
    legend_elements = [
        Patch(facecolor=bar_color, alpha=0.7, label="Growing"),
        Patch(facecolor=hot_color, alpha=0.85, label="Hot (top 20% 3-month)"),
    ]
    ax.legend(handles=legend_elements, loc="lower right")
    fig.tight_layout()
    _plot_and_save(fig, out, theme)


def plot_hot_topics(growth_df: pd.DataFrame, topic_labels: dict[int, str], out: Path, theme: str):
    plt = _setup_style(theme)
    hot = growth_df[growth_df["hot"]].sort_values("growth_3mo", ascending=False)

    if len(hot) == 0:
        log.info("No hot topics to plot")
        return

    fig, ax = plt.subplots(figsize=(9, 4))
    labels_display = [topic_labels.get(t, f"Topic {t}") for t in hot["topic"]]
    hot_color = "#f72585" if theme == "dark" else "#d90429"

    ax.barh(range(len(hot)), hot["growth_3mo"], color=hot_color, alpha=0.8)

    ax.set_yticks(range(len(hot)))
    ax.set_yticklabels(labels_display, fontsize=9)
    ax.set_xlabel("3-month growth coefficient")
    ax.set_title("Hottest topics (last 3 months)", fontsize=14)
    ax.axvline(0, color="#666666", linewidth=0.8)
    fig.tight_layout()
    _plot_and_save(fig, out, theme)


def plot_weekday(df: pd.DataFrame, out: Path, theme: str):
    plt = _setup_style(theme)
    fig, ax = plt.subplots(figsize=(9, 4))
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    counts = df["date_parsed"].dt.dayofweek.value_counts().reindex(range(7), fill_value=0)
    xs = range(7)
    bar_color = "#4361ee" if theme == "dark" else "#4361ee"
    ax.bar(xs, [counts[i] for i in xs], color=bar_color, alpha=0.65, tick_label=days)
    for i, v in enumerate(counts):
        ax.text(i, v + 2, str(int(v)), ha="center", va="bottom", fontsize=9)
    ax.set_title("Papers shared per weekday", fontsize=14)
    ax.set_ylabel("Papers")
    fig.tight_layout()
    _plot_and_save(fig, out, theme)


def plot_hour(df: pd.DataFrame, out: Path, theme: str):
    plt = _setup_style(theme)
    fig, ax = plt.subplots(figsize=(10, 4))
    hours = df["date_parsed"].dt.hour.value_counts().reindex(range(24), fill_value=0)
    xs = range(24)
    bar_color = "#4361ee" if theme == "dark" else "#4361ee"
    ax.bar(xs, [hours[i] for i in xs], color=bar_color, alpha=0.65,
           tick_label=[f"{h:02d}" for h in xs])
    ax.set_title("Papers shared per hour of day", fontsize=14)
    ax.set_xlabel("Hour (UTC)")
    ax.set_ylabel("Papers")
    ax.set_xticks(range(0, 24, 2))
    fig.tight_layout()
    _plot_and_save(fig, out, theme)


def _plot_all(df, emb_2d, labels, topic_labels, trends, growth_df):
    """Generate all SVGs for both themes."""
    for theme in ("dark", "light"):
        plot_umap(emb_2d, labels, topic_labels, OUT_DIR / "umap.svg", theme)
        plot_trends_overall(trends, OUT_DIR / "trends_overall.svg", theme)
        plot_trends_per_topic(trends, topic_labels, growth_df, OUT_DIR / "trends_per_topic.svg", theme)
        plot_growth(growth_df, topic_labels, OUT_DIR / "growth.svg", theme)
        plot_hot_topics(growth_df, topic_labels, OUT_DIR / "hot_topics.svg", theme)
        plot_weekday(df, OUT_DIR / "weekday.svg", theme)
        plot_hour(df, OUT_DIR / "hour.svg", theme)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _cache_key(titles: list[str]) -> bytes:
    import hashlib
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


def main():
    df = load_papers()
    titles = df["title"].tolist()

    emb = _load_or_embed(titles)

    log.info("Step 2/6: Clustering …")
    labels = cluster_and_assign(emb)

    log.info("Step 3/6: Labeling topics …")
    topic_labels = _tfidf_labels(df["title"], labels)
    log.info("Topic labels: %s", topic_labels)

    log.info("Step 4/6: UMAP reduction …")
    emb_2d = reduce_umap(emb)

    log.info("Step 5/6: Computing trends & growth …")
    trends = compute_trends(df, labels)
    growth_df = compute_growth(df, labels)

    log.info("Step 6/6: Plotting SVGs (dark + light) …")
    _plot_all(df, emb_2d, labels, topic_labels, trends, growth_df)

    out_papers = df[["title", "url", "date_parsed"]].copy()
    out_papers["topic"] = labels
    out_papers["topic_label"] = out_papers["topic"].map(topic_labels).fillna("Other")
    out_papers["umap_x"] = emb_2d[:, 0]
    out_papers["umap_y"] = emb_2d[:, 1]
    out_papers.to_csv(OUT_DIR / "paper_topics.csv", index=False)
    log.info("Wrote %s", OUT_DIR / "paper_topics.csv")

    trends.to_csv(OUT_DIR / "topic_trends.csv", index=False)
    log.info("Wrote %s", OUT_DIR / "topic_trends.csv")

    growth_df["topic_label"] = growth_df["topic"].map(topic_labels)
    growth_df.to_csv(OUT_DIR / "topic_growth.csv", index=False)
    log.info("Wrote %s", OUT_DIR / "topic_growth.csv")

    log.info("Done!")


if __name__ == "__main__":
    main()
