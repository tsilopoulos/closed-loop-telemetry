.PHONY: test demo reset mcp

# Use the project venv by default; override with `make test PYTHON=python3`.
PYTHON ?= .venv/bin/python

test:
	$(PYTHON) -m pytest tests/ -q

demo:
	$(PYTHON) -m scenarios.trigger cardinality_explosion --service checkout --label sku_id

reset:
	rm -rf .state && $(PYTHON) -m cli.ctl fleet

mcp:
	$(PYTHON) -m mcp_server.server
