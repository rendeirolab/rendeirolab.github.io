from pathlib import Path
import yaml

from jinja2 import Environment, FileSystemLoader


def main():
    build_all_pages()


def build_all_pages():
    # Get templates to be filled
    template_dir = Path("templates")
    pages = sorted(filter(lambda x: x.stem != "template", template_dir.glob("*.html")))

    # Get content
    content_file = Path("content.yaml")
    content = yaml.safe_load(content_file.open().read())

    # Make sure an entry in the YAML file exists for each page to be rendered
    assert all(page.stem in content for page in pages), "Missing entry in content.yaml"

    environment = Environment(loader=FileSystemLoader(template_dir))

    for page in pages:
        page_name = page.stem
        page_file = Path(page_name).with_suffix(".html")

        page_template = environment.from_string(page.open().read())
        html = page_template.render(**content[page_name])

        with page_file.open("w") as f:
            f.write(html)


if __name__ == "__main__":
    main()
