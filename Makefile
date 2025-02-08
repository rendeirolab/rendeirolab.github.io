clean:
	-rm -r docs

build: clean
	uv run build.py

serve: build
	xdg-open http://0.0.0.0:8000/ & uv run python -m http.server -d docs/

deploy: clean
	git add assets/**/*
	git add -u
	git commit -m "update"
	git push origin main

check:
	gh run list -L 4

.DEFAULT_GOAL := serve

