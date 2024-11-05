clean:
	-rm build/*
	-rm templates/papers.html

build: clean
	python build.py

serve: build
	python -m http.server -d build/

.DEFAULT_GOAL := serve
