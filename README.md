[![Deploy Website](https://github.com/rendeirolab/rendeirolab.github.io/actions/workflows/deploy.yml/badge.svg)](https://github.com/rendeirolab/rendeirolab.github.io/actions/workflows/deploy.yml)

Rendeiro lab website
====================

The website is hosted at Github pages through https://rendeirolab.github.io, and also at https://rendeiro.group/.

## Development and maintenance

The website is extremely simple, using YAML for content and configuration, and [Jinja for templating](https://jinja.palletsprojects.com/), such that content and form are separate and can be edited independently using nothing but a plain text editor.

The source files inside [templates](templates/) are the only ones that should be modified during editing.

[Bootstrap v5](https://getbootstrap.com/) is used to style the website.

A [single Python script](build.py) is used to generate the website using the YAML [config](config.yaml) and [content](content/) files and the [templates](templates/). It generates the static HTML files in the `docs` directory, and these are then served through Github pages.

### Building

The build script is self contained, using `uv script` to install dependencies in an isolated environment.

The only build requirements are `pyyaml`, `jinja2`, `requests`, `beautifulsoup4`, and `markdown2`.

```bash
uv run build.py                          # installs dependencies and builds the website
uv run task collect-papers               # extract new papers from mailbox and enrich
uv run task insights                     # regenerate all topic insights and plots
uv run task insights-cool-papers         # regenerate cool papers insights only
uv run task insights-news                # regenerate news insights only
```

### Serving locally

After editing, you can render the website with `uv run task serve` (from [taskipy](https://github.com/taskipy/taskipy)), which simply runs `python -m http.server` in the `docs` directory.
This includes hot-module reloading, so any changes to the source files will automatically trigger a rebuild and refresh the browser.

### Deploying

If changes look good, commit and push to the `main` branch.

You can run `task deploy` which is equivalent to `git add [...]`, `git commit [...]` and `git push origin main`.

Github pages will build and commit the `docs` html files to the `gh-pages` branch to serve them automatically.
