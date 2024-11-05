clean:
	-rm *.html
	-rm templates/papers.html

build: clean
	python build.py

serve: build
	python -m http.server

.DEFAULT_GOAL := serve
