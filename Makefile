clean:
	-rm -r docs

build: clean
	python build.py

serve: build
	xdg-open http://0.0.0.0:8000/ & python -m http.server -d docs/

deploy: build
	git add -u
	git commit -m "update"
	git push origin main

.DEFAULT_GOAL := serve