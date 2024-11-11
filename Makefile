clean:
	-rm -r docs
	-rm templates/papers.html

build: clean
	python build.py

serve: build
	xdg-open http://0.0.0.0:8000/ & python -m http.server -d docs/

deploy:
	git add -u
	git commit -m "update"
	git push origin main

.DEFAULT_GOAL := serve
