Rendeiro lab website
====================

The website is hosted at Github pages through https://rendeirolab.github.io, and also at https://rendeiro.group/.

# Development and maintenance

The website is extremely simple, using YAML for content and configuration and [Jinja for templating](https://jinja.palletsprojects.com/), such that content and form are separate and can be edited independently using nothing but a plain text editor.

The source files inside [templates](templates/) are the only ones that should be modified during editing.

[Bootstrap v5](https://getbootstrap.com/) is used to style the website.

A [single Python script](build.py) is used to generate the website using [the YAML content file](content.yaml) and the templates, and generating the static HTML files in the root of the repository, which are then served through Github pages.

Before development, remove the rendered files from the root of the repository: `make clean`.

After editing, simply run `make` to re-render the website, commit and push the changes to make them live.
