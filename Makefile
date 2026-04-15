.PHONY: eval test

PY ?= python

eval:
	$(PY) -m eval.run --manifest eval/manifest.json --out eval/reports $(if $(CLIPS),--clips $(CLIPS))

# placeholder — once we add unit tests (stats module first)
test:
	$(PY) -m pytest -q services/dashboard eval
