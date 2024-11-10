clean:
	-rm -r docs

build: clean
	python build.py

serve: build
	python -m http.server -d docs/

deploy: build
	git add -u
	git commit -m "update"
	git push origin main

.DEFAULT_GOAL := serve
