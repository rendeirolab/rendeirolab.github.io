#!/usr/bin/env uv --script

# /// script
# dependencies = [
#   "pyyaml",
#   "jinja2",
#   "requests",
#   "beautifulsoup4",
#   "markdown2",
# ]
# ///

import argparse
from pathlib import Path
import os
import shutil
import xml.etree.cElementTree as ET
from datetime import datetime
from datetime import date as date_class
import subprocess
from copy import deepcopy
import logging

logging.basicConfig(level=logging.WARNING, format="%(message)s")
log = logging.getLogger("build")

import yaml
from jinja2 import Environment, FileSystemLoader
import requests
from bs4 import BeautifulSoup
from markdown2 import Markdown
from collections import defaultdict

config = yaml.safe_load(Path("config.yaml").open().read())
template_dir = Path(config["template_dir"])
content_dir = Path(config["content_dir"])
build_dir = Path(config["build_dir"])
build_dir.mkdir(exist_ok=True, parents=True)


def main():
    build_all_pages()
    make_cool_papers_rss()
    make_news_rss()
    make_sitemap()
    make_robots_txt()


def build_all_pages():
    environment = Environment(loader=FileSystemLoader(template_dir))

    additionals = {"index": ["news"]}

    import csv as csv_mod
    import sys as csv_sys
    csv_mod.field_size_limit(csv_sys.maxsize)

    for page in config["pages"]:
        if page == "manual":
            build_lab_manual()
            continue
        elif page == "posts":
            build_posts()
            continue

        content_file = content_dir / f"{page}.yaml"
        content = load_yaml(content_file)[page]

        if page == "cool_papers":
            papers = _load_cool_papers()
            from collections import defaultdict
            by_year = defaultdict(list)
            for p in papers:
                yr = p.get("date_parsed", p.get("date", ""))[:4]
                by_year[yr].append(p)
            for yr in by_year:
                by_year[yr].sort(
                    key=lambda x: x.get("date_parsed", x.get("date", "")),
                    reverse=True,
                )
            content["papers"] = dict(
                sorted(by_year.items(), reverse=True)
            )

        page_file = build_dir / config["pages"][page]["file"]
        page_file.parent.mkdir(exist_ok=True, parents=True)

        page_template = load_template(environment, config["pages"][page]["template"])

        add = {}
        if page in additionals:
            add = {
                k: load_yaml(content_dir / f"{k}.yaml")[k][k]
                for k in additionals[page]
            }

        html = page_template.render(
            page_url=config["deploy_url"] + config["pages"][page]["url"],
            **config,
            **content,
            **add,
        )

        with page_file.open("w") as f:
            f.write(html)

    build_news_fragments(environment)
    build_cool_papers_fragments(environment)

    # if local, copy assets folder to build
    if not os.getenv("GITHUB_ACTIONS"):
        if (build_dir / "assets").exists():
            shutil.rmtree(build_dir / "assets")
        shutil.copytree("assets", build_dir / "assets")


def build_posts():
    """Build blog-post-like pages from markdown files in content/posts/."""
    posts_dir = content_dir / "posts"
    if not posts_dir.exists():
        return

    environment = Environment(loader=FileSystemLoader(template_dir))
    template = load_template(environment, "post.html")
    md = Markdown(
        extras=[
            "fenced-code-blocks",
            "highlightjs-lang",
            "metadata",
            "tables",
            "footnotes",
        ]
    )

    posts_metadata = []  # Collect metadata for index

    for post_file in posts_dir.glob("*.md"):
        slug = post_file.stem
        raw_content = post_file.read_text()

        html_content = md.convert(raw_content)
        metadata = getattr(md, "metadata", {}) or {}

        page_dir = build_dir / "p" / slug
        page_dir.mkdir(exist_ok=True, parents=True)
        page_file = page_dir / "index.html"

        page_url = f"/p/{slug}/"

        # Collect for index
        posts_metadata.append(
            {
                "slug": slug,
                "url": page_url,
                "title": metadata.get("title", slug.replace("-", " ").title()),
                "subtitle": metadata.get("subtitle"),
                "date": metadata.get("date"),
            }
        )

        html = template.render(
            page_url=config["deploy_url"] + page_url.lstrip("/"),
            title=metadata.get("title", slug.replace("-", " ").title()),
            subtitle=metadata.get("subtitle"),
            date=metadata.get("date"),
            content=html_content,
            **config,
        )

        with page_file.open("w") as f:
            f.write(html)

    # Build index after all posts (even if none)
    build_posts_index(posts_metadata, environment)


def build_posts_index(posts_metadata: list[dict], environment: Environment):
    """Build an index page listing all posts."""
    template = load_template(environment, "posts_index.html")

    page_dir = build_dir / "p"
    page_dir.mkdir(exist_ok=True, parents=True)

    html = template.render(
        page_url=config["deploy_url"] + "p/",
        title="Posts",
        posts=sorted(posts_metadata, key=lambda x: x.get("date") or "", reverse=True),
        **config,
    )

    with (page_dir / "index.html").open("w") as f:
        f.write(html)


def build_news_fragments(environment):
    """Generate per-year HTML fragments for htmx lazy-loading on the news page."""
    content = load_yaml(content_dir / "news.yaml")["news"]

    fragment_template = load_template(environment, "_news_year.html")

    sorted_news = sorted(
        content["news"],
        key=lambda x: x["date"],
        reverse=True,
    )

    years = defaultdict(list)
    for item in sorted_news:
        years[item["date"].year].append(item)

    for year in sorted(years.keys(), reverse=True):
        html = fragment_template.render(
            items=years[year],
            static_url=config["static_url"],
        )
        fragment_path = build_dir / "news" / f"year-{year}.html"
        fragment_path.parent.mkdir(exist_ok=True, parents=True)
        with fragment_path.open("w") as f:
            f.write(html)


def _load_cool_papers():
    """Read papers.csv and add from_name via team email lookup."""
    import csv as csv_mod
    import sys as csv_sys

    csv_mod.field_size_limit(csv_sys.maxsize)
    csv_path = Path(__file__).parent / "cool-papers" / "papers.csv"
    if not csv_path.exists():
        return []

    team_data = load_yaml(content_dir / "team.yaml").get("team", {})
    members = team_data.get("members", {})
    email_to_name = {}
    for m in members.values():
        email = m.get("email")
        if email:
            email_to_name[email.lower()] = m["name"]

    papers = []
    with csv_path.open() as f:
        for row in csv_mod.DictReader(f):
            row = dict(row)
            from_email = row.get("from", "")
            local = from_email.split("@")[0]
            email_lower = from_email.lower()
            row["from_name"] = email_to_name.get(email_lower, local)
            papers.append(row)
    return papers


def build_cool_papers_fragments(environment):
    """Generate per-year HTML fragments for htmx lazy-loading on the cool_papers page."""
    papers = _load_cool_papers()
    if not papers:
        return

    by_year = defaultdict(list)
    for p in papers:
        yr = p.get("date_parsed", p.get("date", ""))[:4]
        by_year[yr].append(p)
    for yr in by_year:
        by_year[yr].sort(
            key=lambda x: x.get("date_parsed", x.get("date", "")),
            reverse=True,
        )

    fragment_template = load_template(environment, "_cool_papers_year.html")

    for year in sorted(by_year.keys(), reverse=True):
        html = fragment_template.render(year_papers=by_year[year])
        fragment_path = build_dir / "cool-papers" / f"year-{year}.html"
        fragment_path.parent.mkdir(exist_ok=True, parents=True)
        with fragment_path.open("w") as f:
            f.write(html)


def build_lab_manual():
    name = config["pages"]["manual"]["url"][1:-1]
    environment = Environment(loader=FileSystemLoader(template_dir))
    template = load_template(environment, config["pages"]["manual"]["template"])

    manual_root_url = config["pages"]["manual"]["url"]
    req = requests.get(
        f"https://raw.githubusercontent.com/{config['manual_repo']}/refs/heads/main/Makefile"
    )
    page_order = [
        p.split(".md")[0]
        for p in req.content.decode().split("\n")
        if p.startswith("source/") or p.endswith(".md \\")
    ]

    pages = dict()
    for page in page_order:
        if page == "README":
            page_slug = "index"
            page_file = build_dir / name / "index.html"
            page_url = f"/{name}/"
        else:
            page_slug = page.replace("source/", "").lower()
            page_file = build_dir / name / page_slug / "index.html"
            page_url = f"/{name}/{page_slug}/"
        page_file.parent.mkdir(exist_ok=True, parents=True)
        req = requests.get(
            f"https://raw.githubusercontent.com/{config['manual_repo']}/refs/heads/main/{page}.md"
        )
        html = Markdown(
            extras=[
                "fenced-code-blocks",
                "highlightjs-lang",
                "task_list",
                "admonitions",
                "tables",
            ]
        ).convert(req.content.decode())
        body = BeautifulSoup(html, "html.parser")
        page_title = body.find("h1").text
        if page_slug == "index":
            body.find("h1").decompose()

        # Replace internal markdown links
        links = body.find_all("a")
        for link in links:
            href = link.get("href")
            if href.endswith(".md"):
                link["href"] = f"{manual_root_url}{href.replace('.md', '')}"

        link = (
            f"https://github.com/{config['manual_repo']}/edit/refs/heads/main/{page}.md"
        )
        msg = f"<hr><p><small>Edit this page on <a href='{link}'>GitHub</a></small></p>"
        snippet = BeautifulSoup(msg, "html.parser").extract()
        body.append(snippet)
        pages[page_slug] = dict(
            page_url=page_url,
            page_slug=page_slug,
            page_title=page_title,
            file=page_file,
            page_content=str(body),
            active=False,
        )

    for page_slug, data in pages.items():
        manual_pages = deepcopy(pages)
        manual_pages[page_slug]["active"] = True
        html = template.render(
            page_url=data["page_url"],
            page_title=data["page_title"],
            page_content=data["page_content"],
            manual_pages=manual_pages,
            manual_root_url=manual_root_url,
            **config,
        )

        with data["file"].open("w") as f:
            f.write(html)


def get_last_mod_date() -> dict[str, str]:
    now = today()

    try:
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        print("Not a git repository. Using current date as last modification date.")
        return {page.stem: now for page in content_dir.glob("*.yaml")}

    last_mod_dates = {}

    # Content YAML files
    for page in content_dir.glob("*.yaml"):
        page_name = page.stem
        date_obj = git_log_date(page)
        last_mod_dates[page_name] = date_obj.strftime("%Y-%m-%d") if date_obj else now

    # Posts (markdown files)
    posts_dir = content_dir / "posts"
    if posts_dir.exists():
        latest_post_date = None
        for post in posts_dir.glob("*.md"):
            post_key = f"post:{post.stem}"
            date_obj = git_log_date(post)
            if date_obj:
                last_mod_dates[post_key] = date_obj.strftime("%Y-%m-%d")
                if latest_post_date is None or date_obj > latest_post_date:
                    latest_post_date = date_obj
            else:
                last_mod_dates[post_key] = now

        if latest_post_date:
            last_mod_dates["posts"] = latest_post_date.strftime("%Y-%m-%d")

    return last_mod_dates


def get_manual_mod_dates() -> dict[str, str]:
    """Clone manual repo and get git log dates."""
    import tempfile

    now = today()
    mod_dates = {}
    repo = config["manual_repo"]

    with tempfile.TemporaryDirectory() as tmpdir:
        subprocess.run(
            ["git", "clone", f"https://github.com/{repo}.git", tmpdir],
            check=True,
            capture_output=True,
        )

        # Get page order from Makefile (same logic as build_manual)
        makefile = Path(tmpdir) / "Makefile"
        page_order = [
            p.split(".md")[0]
            for p in makefile.read_text().split("\n")
            if p.startswith("source/") or p.endswith(".md \\")
        ]

        latest_manual_date = None

        for page in page_order:
            if page == "README":
                page_slug = "index"
                file_path = "README.md"
            else:
                page_slug = page.replace("source/", "").lower()
                file_path = f"{page}.md"

            date_obj = git_log_date(file_path, git_dir=tmpdir)
            if date_obj:
                mod_dates[f"lab-manual:{page_slug}"] = date_obj.strftime("%Y-%m-%d")
                if latest_manual_date is None or date_obj > latest_manual_date:
                    latest_manual_date = date_obj
            else:
                log.debug("Could not get git log date for %s, using current date", file_path)
                mod_dates[f"lab-manual:{page_slug}"] = now

        if latest_manual_date:
            mod_dates["lab-manual"] = latest_manual_date.strftime("%Y-%m-%d")

    return mod_dates


def make_sitemap():
    now = today()
    mod_dates = get_last_mod_date() | get_manual_mod_dates()

    # Build reverse mapping: build file path -> page key
    file_to_page = {config["pages"][page]["file"]: page for page in config["pages"]}

    sitemap = ET.Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")

    for page in build_dir.glob("**/*.html"):
        relative_path = str(page.relative_to(build_dir))

        # Skip HTMX fragment files (not standalone pages)
        if relative_path.startswith("news/year-") or relative_path.startswith("cool-papers/year-"):
            continue

        page_key = file_to_page.get(relative_path)

        # Handle posts
        if (
            page_key is None
            and relative_path.startswith("p/")
            and relative_path.count("/") == 2
        ):
            post_slug = relative_path.split("/")[1]
            page_key = f"post:{post_slug}"
        # Handle manual pages
        elif page_key is None and relative_path.startswith("lab-manual/"):
            manual_page_slug = relative_path.replace("manual/", "").replace(
                "/index.html", ""
            )
            if manual_page_slug == "index":
                manual_page_slug = "index"
            else:
                manual_page_slug = manual_page_slug.replace("lab-", "")
            page_key = f"lab-manual:{manual_page_slug}"

        url = config["deploy_url"] + relative_path.replace("index.html", "")
        url_element = ET.SubElement(sitemap, "url")
        ET.SubElement(url_element, "loc").text = url

        if page_key and page_key in mod_dates:
            ET.SubElement(url_element, "lastmod").text = mod_dates[page_key]
        else:
            log.debug("No last modification date found for %s, using current date", relative_path)
            ET.SubElement(url_element, "lastmod").text = now

    # Add RSS feeds to sitemap
    for feed_rel in ["cool-papers/feed.xml", "news/feed.xml"]:
        feed_path = build_dir / feed_rel
        if feed_path.exists():
            url_element = ET.SubElement(sitemap, "url")
            ET.SubElement(url_element, "loc").text = f"{config['deploy_url'].rstrip('/')}/{feed_rel}"
            ET.SubElement(url_element, "lastmod").text = now

    tree = ET.ElementTree(sitemap)
    ET.indent(tree, space="\t", level=0)
    with open(build_dir / "sitemap.xml", "wb") as f:
        tree.write(f, encoding="utf-8", xml_declaration=True, method="xml")


def make_robots_txt():
    txt = f"Sitemap: {config['deploy_url']}sitemap.xml"
    with open(build_dir / "robots.txt", "w") as f:
        f.write(txt)


def make_cool_papers_rss():
    """Generate an RSS 2.0 feed for cool papers."""
    papers = _load_cool_papers()
    if not papers:
        return

    papers.sort(
        key=lambda p: p.get("date_parsed", p.get("date", "")),
        reverse=True,
    )

    import email.utils
    from email.utils import format_datetime

    ET.register_namespace("atom", "http://www.w3.org/2005/Atom")

    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")

    deploy_url = config["deploy_url"].rstrip("/")
    feed_url = f"{deploy_url}/cool-papers/feed.xml"
    papers_url = f"{deploy_url}/cool-papers/"

    ET.SubElement(channel, "title").text = "Cool Papers — Rendeiro Lab"
    ET.SubElement(channel, "link").text = papers_url
    ET.SubElement(channel, "description").text = "Recent interesting papers shared by the Rendeiro Lab team"
    ET.SubElement(channel, "language").text = "en-us"
    atom_link = ET.SubElement(channel, "{http://www.w3.org/2005/Atom}link")
    atom_link.set("href", feed_url)
    atom_link.set("rel", "self")
    atom_link.set("type", "application/rss+xml")
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now())

    for paper in papers[:100]:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = paper.get("title", "")
        ET.SubElement(item, "link").text = paper.get("url", papers_url)
        ET.SubElement(item, "guid", isPermaLink="false").text = paper.get("url", "") or paper.get("doi", "") or paper.get("title", "")

        if paper.get("from_name"):
            ET.SubElement(item, "author").text = paper["from_name"]

        pub_date = paper.get("date_parsed", paper.get("date", ""))
        try:
            dt = datetime.fromisoformat(pub_date)
            ET.SubElement(item, "pubDate").text = format_datetime(dt)
        except (ValueError, TypeError):
            pass

        comment = paper.get("comment", "")
        if comment:
            desc = comment.split("^^", 1)[-1].lstrip("^ ")[:500]
        else:
            desc = ""
        if paper.get("journal"):
            desc = f"[{paper['journal']}] {desc}" if desc else f"[{paper['journal']}]"
        ET.SubElement(item, "description").text = desc

    rss_path = build_dir / "cool-papers" / "feed.xml"
    rss_path.parent.mkdir(exist_ok=True)
    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ", level=0)
    with rss_path.open("wb") as f:
        tree.write(f, encoding="utf-8", xml_declaration=True, method="xml")


def make_news_rss():
    """Generate an RSS 2.0 feed for lab news."""
    content = load_yaml(content_dir / "news.yaml")["news"]
    items = content.get("news", [])
    if not items:
        return

    sorted_news = sorted(items, key=lambda x: x["date"], reverse=True)

    from email.utils import format_datetime

    ET.register_namespace("atom", "http://www.w3.org/2005/Atom")

    rss = ET.Element("rss", version="2.0")
    channel = ET.SubElement(rss, "channel")

    deploy_url = config["deploy_url"].rstrip("/")
    feed_url = f"{deploy_url}/news/feed.xml"
    news_url = f"{deploy_url}/news/"

    ET.SubElement(channel, "title").text = "News — Rendeiro Lab"
    ET.SubElement(channel, "link").text = news_url
    ET.SubElement(channel, "description").text = "Recent news from the Rendeiro Lab"
    ET.SubElement(channel, "language").text = "en-us"
    atom_link = ET.SubElement(channel, "{http://www.w3.org/2005/Atom}link")
    atom_link.set("href", feed_url)
    atom_link.set("rel", "self")
    atom_link.set("type", "application/rss+xml")
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now())

    for item in sorted_news[:25]:
        rss_item = ET.SubElement(channel, "item")
        ET.SubElement(rss_item, "title").text = item.get("title", "")

        guid = item.get("title", "") + str(item.get("date", ""))
        ET.SubElement(rss_item, "guid", isPermaLink="false").text = guid

        pub_date = item.get("date")
        if isinstance(pub_date, datetime):
            pub_dt = pub_date
        elif isinstance(pub_date, date_class):
            pub_dt = datetime(pub_date.year, pub_date.month, pub_date.day)
        else:
            try:
                pub_dt = datetime.fromisoformat(str(pub_date))
            except (ValueError, TypeError):
                pub_dt = datetime.now()
        ET.SubElement(rss_item, "pubDate").text = format_datetime(pub_dt)

        desc = item.get("description", "")
        if desc:
            from bs4 import BeautifulSoup
            text = BeautifulSoup(desc, "html.parser").get_text(separator=" ", strip=True)
            ET.SubElement(rss_item, "description").text = text[:500]

    rss_path = build_dir / "news" / "feed.xml"
    rss_path.parent.mkdir(exist_ok=True)
    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ", level=0)
    with rss_path.open("wb") as f:
        tree.write(f, encoding="utf-8", xml_declaration=True, method="xml")


def clean_build_dir():
    shutil.rmtree(build_dir)


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def load_yaml(path: str | Path) -> dict:
    return yaml.safe_load(Path(path).read_text())


def load_template(environment: Environment, name: str):
    return environment.from_string((template_dir / name).read_text())


def parse_git_date(date_str: str) -> datetime:
    return datetime.fromisoformat(
        date_str.replace("Z", "+00:00")
        .replace(" +0100", " +01:00")
        .replace(" +0200", " +02:00")
    )


def git_log_date(
    file_path: str | Path, git_dir: str | Path | None = None
) -> datetime | None:
    cmd = ["git", "log", "-n", "1", "--format=%ci", "--", str(file_path)]
    if git_dir is not None:
        cmd = ["git", "-C", str(git_dir)] + cmd
    try:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        date_str = result.stdout.strip()
        if not date_str:
            return None
        return parse_git_date(date_str)
    except subprocess.CalledProcessError:
        return None


def serve():
    from livereload import Server

    main()

    server = Server()

    server.watch("templates/", lambda: main())
    server.watch("content/", lambda: main())
    server.watch("assets/", lambda: main())
    server.watch("config.yaml", lambda: main())

    print("Dev server at http://localhost:8000 — watching for changes...")
    server.serve(root=build_dir, port=8000, open_url_delay=1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--serve", action="store_true", help="Start dev server with live reload"
    )
    args = parser.parse_args()

    if args.serve:
        serve()
    else:
        main()
