clean:
	-rm *.html

build: clean
	python build.py

serve: build
	python -m http.server
