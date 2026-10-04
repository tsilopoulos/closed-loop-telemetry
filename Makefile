.PHONY: setup test demo reset mcp agent replay

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

# Empties the DB in place rather than deleting it, so an already-running MCP
# server (e.g. your Claude Code session) sees the reset immediately.
reset:
	$(PYTHON) -m cli.ctl reset && $(PYTHON) -m cli.ctl fleet

mcp:
	$(PYTHON) -m mcp_server.server

# The on-stage agent: Claude Code with the otel-fleet MCP tools and nothing
# else (no shell, no file edits), so it cannot reach `ctl approve` or edit
# guardrails.yaml. See .claude/agent-sandbox.json.
agent:
	claude --settings .claude/agent-sandbox.json --strict-mcp-config --mcp-config .mcp.json

# Demo fallback without an LLM: scripted agent side over the real MCP tools,
# then the real interactive `ctl approve`. Record it as the backup video.
replay:
	$(PYTHON) -m scenarios.replay
