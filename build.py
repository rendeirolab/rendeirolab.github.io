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
import subprocess
from copy import deepcopy

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
    make_sitemap()
    make_robots_txt()


def build_all_pages():
    environment = Environment(loader=FileSystemLoader(template_dir))

    additionals = {"index": ["news"]}

    for page in config["pages"]:
        if page == "manual":
            build_lab_manual()
            continue
        elif page == "posts":
            build_posts()
            continue

        content_file = content_dir / f"{page}.yaml"
        content = load_yaml(content_file)[page]

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
    """Clone manual repo shallowly and get git log dates."""
    import tempfile

    now = today()
    mod_dates = {}
    repo = config["manual_repo"]

    with tempfile.TemporaryDirectory() as tmpdir:
        subprocess.run(
            ["git", "clone", "--depth", "50", f"https://github.com/{repo}.git", tmpdir],
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
                print(f"Error getting git log for {file_path}")
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
            print(
                f"No last modification date found for {relative_path}. Using current date."
            )
            ET.SubElement(url_element, "lastmod").text = now

    tree = ET.ElementTree(sitemap)
    ET.indent(tree, space="\t", level=0)
    with open(build_dir / "sitemap.xml", "wb") as f:
        tree.write(f, encoding="utf-8", xml_declaration=True, method="xml")


def make_robots_txt():
    txt = f"Sitemap: {config['deploy_url']}sitemap.xml"
    with open(build_dir / "robots.txt", "w") as f:
        f.write(txt)


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
