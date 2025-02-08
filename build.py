from pathlib import Path
import os
import shutil

import yaml
from jinja2 import Environment, FileSystemLoader


config = yaml.safe_load(Path("config.yaml").open().read())
template_dir = Path(config["template_dir"])
build_dir = Path(config["build_dir"])
build_dir.mkdir(exist_ok=True, parents=True)


def main():
    build_all_pages()
    make_sitemap()
    make_robots_txt()


def build_all_pages():
    # Get templates to be filled
    pages = sorted(filter(lambda x: x.stem != "template", template_dir.glob("*.html")))

    # Get content
    content_file = Path("content.yaml")
    content = yaml.safe_load(content_file.open().read())

    # Make sure an entry in the YAML file exists for each page to be rendered
    assert all(page.stem in content for page in pages), "Missing entry in content.yaml"

    environment = Environment(loader=FileSystemLoader(template_dir))

    additionals = {"index": ["news"]}

    for page in pages:
        if page.stem == config["manual_name"]:
            build_lab_manual()
            continue
        page_name = page.stem
        if page_name != "index":
            page_file = build_dir / page_name / "index.html"
        else:
            page_file = build_dir / "index.html"
        page_file.parent.mkdir(exist_ok=True, parents=True)

        page_template = environment.from_string(page.open().read())

        add = {}
        if page_name in additionals:
            add = {k: content[k][k] for k in additionals[page_name]}

        html = page_template.render(
            page_url=config["deploy_url"] + page_name,
            **config,
            **content[page_name],
            **add,
        )

        with page_file.open("w") as f:
            f.write(html)

    # if local, copy assets folder to build
    if not os.getenv("GITHUB_ACTIONS"):
        if (build_dir / "assets").exists():
            shutil.rmtree(build_dir / "assets")
        shutil.copytree("assets", build_dir / "assets")


def build_lab_manual():
    import tempfile
    from copy import deepcopy as copy
    import requests
    from bs4 import BeautifulSoup
    from yamper import to_html

    name = config["manual_name"]
    environment = Environment(loader=FileSystemLoader(template_dir))
    template = environment.from_string((template_dir / f"{name}.html").open().read())

    manual_root_url = f"/{name}/"
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
            page_slug = page.replace("source/", "")
            page_file = build_dir / name / page_slug / "index.html"
            page_url = f"/{name}/{page_slug}/"
        page_file.parent.mkdir(exist_ok=True, parents=True)
        req = requests.get(
            f"https://raw.githubusercontent.com/{config['manual_repo']}/refs/heads/main/{page}.md"
        )

        with tempfile.NamedTemporaryFile() as f:
            f.write(req.content)
            f.seek(0)
            html = to_html(f.name)
        soup = BeautifulSoup(html, "html.parser")
        body = soup.find("body").div
        page_title = body.find("h1").text
        if page_slug == "index":
            body.find("h1").decompose()
        pages[page] = dict(
            page_url=page_url,
            page_slug=page_slug,
            page_title=page_title,
            file=page_file,
            page_content=str(body),
            active=False,
        )

    for page, data in pages.items():
        manual_pages = copy(pages)
        manual_pages[page]["active"] = True
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
    import subprocess
    from datetime import datetime

    now = datetime.now().strftime("%Y-%m-%d")

    try:
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            check=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError:
        print("Not a git repository. Using current date as last modification date.")
        return {page.stem: now for page in template_dir.glob("*.html")}

    last_mod_dates = {}
    for page in template_dir.glob("*.html"):
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
            date_obj = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
            formatted_date = date_obj.strftime("%Y-%m-%d")
            last_mod_dates[page_name] = formatted_date
        except subprocess.CalledProcessError as e:
            print(f"Error getting git log for {page}: {e}")
            last_mod_dates[page_name] = now
    return last_mod_dates


def make_sitemap():
    import xml.etree.cElementTree as ET
    from datetime import datetime

    now = datetime.now().strftime("%Y-%m-%d")

    mod_dates = get_last_mod_date()

    sitemap = ET.Element("urlset", xmlns="http://www.sitemaps.org/schemas/sitemap/0.9")

    for page in build_dir.glob("**/*.html"):
        page_name = page.parent.name if page.parent != build_dir else page.stem
        url = config["deploy_url"] + str(page.relative_to(build_dir))
        url_element = ET.SubElement(sitemap, "url")
        ET.SubElement(url_element, "loc").text = url
        try:
            ET.SubElement(url_element, "lastmod").text = mod_dates[page_name]
        except KeyError:
            # new page
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
