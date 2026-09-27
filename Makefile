.PHONY: setup test demo reset mcp

# Use the project venv by default; override with `make test PYTHON=python3`.
# .mcp.json also points at .venv/bin/python, so run `make setup` once.
PYTHON ?= .venv/bin/python

setup:
	python3 -m venv .venv
	.venv/bin/python -m pip install -q -r requirements-dev.txt

test:
	$(PYTHON) -m pytest tests/ -q

demo:
	$(PYTHON) -m scenarios.trigger cardinality_explosion --service checkout --label sku_id

reset:
	rm -rf .state && $(PYTHON) -m cli.ctl fleet

mcp:
	$(PYTHON) -m mcp_server.server
