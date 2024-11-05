clean:
	-rm docs/*
	-rm templates/papers.html

build: clean
	python build.py

serve: build
	python -m http.server -d docs/

.DEFAULT_GOAL := serve
