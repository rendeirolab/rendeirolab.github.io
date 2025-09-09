[![Deploy Website](https://github.com/rendeirolab/rendeirolab.github.io/actions/workflows/deploy.yml/badge.svg)](https://github.com/rendeirolab/rendeirolab.github.io/actions/workflows/deploy.yml)

Rendeiro lab website
====================

The website is hosted at Github pages through https://rendeirolab.github.io, and also at https://rendeiro.group/.

# Development and maintenance

The website is extremely simple, using YAML for content and configuration, and [Jinja for templating](https://jinja.palletsprojects.com/), such that content and form are separate and can be edited independently using nothing but a plain text editor.

The source files inside [templates](templates/) are the only ones that should be modified during editing.

[Bootstrap v5](https://getbootstrap.com/) is used to style the website.

A [single Python script](build.py) is used to generate the website using the YAML [config](config.yaml) and [content](content.yaml) files and the [templates](templates/). It generates the static HTML files in the `docs` directory, and these are then served through Github pages.

The only [requirements](requirements.txt) are `pyyaml` and `jinja`.

After editing, you can render the website with `task serve` (from [taskipy](https://github.com/taskipy/taskipy)), which simply runs `python -m http.server` in the `docs` directory.

If changes look good, commit and push (`task deploy` or `git add [...]`, `git commit [...]` and `git push origin main`) to make them live. Github pages will build and commit the `docs` html files to the `gh-pages` branch to serve.
