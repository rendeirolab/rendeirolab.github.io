from pathlib import Path
import yaml

import jinja2


def main():
    build_all_pages()


def build_all_pages():
    template_file = Path("_template.html")
    content_file = Path("content.yaml")

    pages = sorted(filter(lambda x: x.stem != "_template", Path(".").glob("_*.html")))
    content = content_file.open().read()

    # Make sure an entry in the YAML file exists for each page to be rendered
    assert all(
        page.stem[1:] in content for page in pages
    ), "Missing entry in content.yaml"

    environment = jinja2.Environment()
    template = environment.from_string(template_file.open().read())

    for page in pages:
        page_name = page.stem[1:]
        page_file = page.with_stem(page_name)

        # # First render the page-specific template from the YAML content
        page_template = environment.from_string(page.open().read())
        page_content = yaml.safe_load(content_file.open().read())[page_name]
        html = page_template.render(**page_content)

        # Then, render the page-specific content onto the global template
        render = template.render(
            page_name=page_name,
            page_title=page_name.capitalize(),
            page_content=html,
        )
        with page_file.open("w") as f:
            f.write(render)


if __name__ == "__main__":
    main()
