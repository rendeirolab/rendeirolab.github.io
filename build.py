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

from pathlib import Path
import os
import shutil
import xml.etree.cElementTree as ET
from datetime import datetime
import subprocess
from copy import deepcopy as copy

import yaml
from jinja2 import Environment, FileSystemLoader
import requests
from bs4 import BeautifulSoup
from markdown2 import Markdown

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
        content = yaml.safe_load(content_file.open().read())[page]

        page_file = build_dir / config["pages"][page]["file"]
        page_file.parent.mkdir(exist_ok=True, parents=True)

        page_template = environment.from_string(
            (template_dir / config["pages"][page]["template"]).open().read()
        )

        add = {}
        if page in additionals:
            add = {
                k: yaml.safe_load((content_dir / f"{k}.yaml").open().read())[k][k]
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
    template = environment.from_string((template_dir / "post.html").open().read())
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
    template = environment.from_string(
        (template_dir / "posts_index.html").open().read()
    )

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


def build_lab_manual():
    name = config["pages"]["manual"]["url"][1:-1]
    environment = Environment(loader=FileSystemLoader(template_dir))
    template = environment.from_string(
        (template_dir / config["pages"]["manual"]["template"]).open().read()
    )

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
        manual_pages = copy(pages)
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
    now = datetime.now().strftime("%Y-%m-%d")

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
        try:
            result = subprocess.run(
                ["git", "log", "-n", "1", "--format=%ci", "--", page],
                check=True,
                capture_output=True,
                text=True,
            )
            date_str = result.stdout.strip()
            if not date_str:
                continue
            date_obj = datetime.fromisoformat(
                date_str.replace("Z", "+00:00")
                .replace(" +0100", " +01:00")
                .replace(" +0200", " +02:00")
            )
            last_mod_dates[page_name] = date_obj.strftime("%Y-%m-%d")
        except subprocess.CalledProcessError as e:
            print(f"Error getting git log for {page}: {e}")
            last_mod_dates[page_name] = now

    # Posts (markdown files)
    posts_dir = content_dir / "posts"
    if posts_dir.exists():
        latest_post_date = None
        for post in posts_dir.glob("*.md"):
            post_key = f"post:{post.stem}"  # e.g., "post:my-first-post"
            try:
                result = subprocess.run(
                    ["git", "log", "-n", "1", "--format=%ci", "--", post],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                date_str = result.stdout.strip()
                if not date_str:
                    continue
                date_obj = datetime.fromisoformat(
                    date_str.replace("Z", "+00:00")
                    .replace(" +0100", " +01:00")
                    .replace(" +0200", " +02:00")
                )
                formatted_date = date_obj.strftime("%Y-%m-%d")
                last_mod_dates[post_key] = formatted_date

                # Track latest post date for posts index
                if latest_post_date is None or date_obj > latest_post_date:
                    latest_post_date = date_obj
            except subprocess.CalledProcessError as e:
                print(f"Error getting git log for {post}: {e}")
                last_mod_dates[post_key] = now

        # Posts index uses the date of the most recently modified post
        if latest_post_date:
            last_mod_dates["posts"] = latest_post_date.strftime("%Y-%m-%d")

    return last_mod_dates


# def get_manual_mod_dates() -> dict[str, str]:
#     """Get last modification dates for lab-manual pages via GitHub API."""
#     now = datetime.now().strftime("%Y-%m-%d")
#     mod_dates = {}

#     repo = config["manual_repo"]
#     api_base = f"https://api.github.com/repos/{repo}/commits"

#     req = requests.get(
#         f"https://api.github.com/repos/{repo}/contents/source",
#         headers={"Accept": "application/vnd.github.v3+json"},
#     )
#     if req.status_code != 200:
#         return {}

#     for file_info in req.json():
#         if not file_info["name"].endswith(".md"):
#             continue
#         page_name = file_info["name"].replace(".md", "")

#         # Get last commit for this file
#         commits_req = requests.get(
#             api_base,
#             params={"path": f"source/{file_info['name']}", "per_page": 1},
#             headers={"Accept": "application/vnd.github.v3+json"},
#         )
#         if commits_req.status_code == 200 and commits_req.json():
#             commit_date = commits_req.json()[0]["commit"]["committer"]["date"]
#             date_obj = datetime.fromisoformat(commit_date.replace("Z", "+00:00"))
#             mod_dates[f"lab-manual:{page_name}"] = date_obj.strftime("%Y-%m-%d")
#         else:
#             mod_dates[f"lab-manual:{page_name}"] = now

#     return mod_dates


def get_manual_mod_dates() -> dict[str, str]:
    """Clone manual repo shallowly and get git log dates."""
    import tempfile

    now = datetime.now().strftime("%Y-%m-%d")
    mod_dates = {}
    repo = config["manual_repo"]

    with tempfile.TemporaryDirectory() as tmpdir:
        # Shallow clone with enough history to get meaningful dates
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

            try:
                result = subprocess.run(
                    [
                        "git",
                        "-C",
                        tmpdir,
                        "log",
                        "-n",
                        "1",
                        "--format=%ci",
                        "--",
                        file_path,
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                date_str = result.stdout.strip()
                if not date_str:
                    mod_dates[f"lab-manual:{page_slug}"] = now
                    continue

                date_obj = datetime.fromisoformat(
                    date_str.replace("Z", "+00:00")
                    .replace(" +0100", " +01:00")
                    .replace(" +0200", " +02:00")
                )
                formatted_date = date_obj.strftime("%Y-%m-%d")
                mod_dates[f"lab-manual:{page_slug}"] = formatted_date

                # Track latest for manual index
                if latest_manual_date is None or date_obj > latest_manual_date:
                    latest_manual_date = date_obj

            except subprocess.CalledProcessError as e:
                print(f"Error getting git log for {file_path}: {e}")
                mod_dates[f"lab-manual:{page_slug}"] = now

        # Manual index uses the most recently modified page
        if latest_manual_date:
            mod_dates["lab-manual"] = latest_manual_date.strftime("%Y-%m-%d")

    return mod_dates


def make_sitemap():
    now = datetime.now().strftime("%Y-%m-%d")
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


if __name__ == "__main__":
    main()
