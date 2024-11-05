clean:
	-rm docs/*
	-rm templates/papers.html

build: clean
	python build.py

serve: build
	python -m http.server -d docs/

deploy:
	git add -u
	git commit -m "update"
	git push origin gh-pages

.DEFAULT_GOAL := serve
