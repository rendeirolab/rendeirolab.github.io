from pathlib import Path
import yaml
import os
import shutil

from jinja2 import Environment, FileSystemLoader
import requests
import bs4


config = yaml.safe_load(Path("config.yaml").open().read())
template_dir = Path(config["template_dir"])
build_dir = Path(config["build_dir"])
build_dir.mkdir(exist_ok=True)
static_url = config["static_url"]


def main():
    get_publications()
    build_all_pages()


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
        page_name = page.stem
        if page_name == "papers":
            continue
        if page_name != "index":
            page_file = build_dir / page_name / "index.html"
        else:
            page_file = build_dir / "index.html"
        page_file.parent.mkdir(exist_ok=True)

        page_template = environment.from_string(page.open().read())

        add = {}
        if page_name in additionals:
            add = {k: content[k][k] for k in additionals[page_name]}

        html = page_template.render(static_url=static_url, **content[page_name], **add)

        with page_file.open("w") as f:
            f.write(html)

    # if local, copy assets folder to build
    if not os.getenv("GITHUB_ACTIONS"):
        shutil.copytree("assets", build_dir / "assets")


def get_publications():
    source = "https://andre-rendeiro.com"
    html = requests.get(source).content
    soup = bs4.BeautifulSoup(html, "lxml")
    pub_list = soup.find_all("ol")[-1]
    with open(template_dir / "papers.html", "w") as f:
        f.write(str(pub_list))


if __name__ == "__main__":
    main()
