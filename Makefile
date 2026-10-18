PYTHON ?= python3
.PHONY: test format-check

test:
	$(PYTHON) -m unittest discover -s tests -v

format-check:
	$(PYTHON) -m black --check --line-length 100 scripts tests
