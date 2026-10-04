.PHONY: help setup test mcp agent reset demo injection growth incident clear \
        replay list show approve reject rollback audit fleet

# Use the project venv by default; override with `make test PYTHON=python3`.
# .mcp.json also points at .venv/bin/python, so run `make setup` once.
PYTHON ?= .venv/bin/python
CTL := $(PYTHON) -m cli.ctl
TRIGGER := $(PYTHON) -m scenarios.trigger

# Fail early with a usage hint when a target needs ID=<...>.
need-id = $(if $(ID),,$(error Usage: make $@ ID=<$(1)>))

help: ## List targets
	@grep -hE '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*## "}; {printf "  make %-10s %s\n", $$1, $$2}'

# --- setup ----------------------------------------------------------------------

setup: ## Create .venv and install deps (Python 3.11+; .mcp.json needs it)
	python3 -m venv .venv
	.venv/bin/python -m pip install -q -r requirements-dev.txt

test: ## Run the test suite
	$(PYTHON) -m pytest tests/ -q

mcp: ## Run the MCP server on stdio (MCP clients normally start it)
	$(PYTHON) -m mcp_server.server

# --- the agent --------------------------------------------------------------------

# The on-stage agent: Claude Code with the otel-fleet MCP tools and nothing
# else (no shell, no file edits), so it cannot reach `ctl approve` or edit
# guardrails.yaml. See .claude/agent-sandbox.json. Needs a real terminal.
agent: ## Start Claude Code with ONLY the otel-fleet MCP tools
	claude --settings .claude/agent-sandbox.json --strict-mcp-config --mcp-config .mcp.json

# --- scenarios ----------------------------------------------------------------------

# Empties the DB in place rather than deleting it, so an already-running MCP
# server (e.g. your Claude Code session) sees the reset immediately.
reset: ## Fresh state: clear proposals/audit, fleet back to baseline
	$(CTL) reset && $(CTL) fleet

demo: ## Scenario: cardinality explosion in checkout (sku_id, ~8x)
	$(TRIGGER) cardinality_explosion --service checkout --label sku_id

injection: ## Scenario: explosion + log lines telling the agent to move the exporter
	$(TRIGGER) prompt_injection --service checkout

growth: ## Scenario: organic traffic growth; the right answer is no proposal
	$(TRIGGER) traffic_growth

incident: ## Scenario: upstream timeouts in fulfillment
	$(TRIGGER) incident --service fulfillment

clear: ## Clear the active scenario (keeps proposals and audit)
	$(TRIGGER) clear

replay: ## Demo fallback without an LLM: scripted agent, then real ctl approve
	$(PYTHON) -m scenarios.replay

# --- the human side -----------------------------------------------------------------

list: ## List proposals (STATUS=pending_approval to filter)
	$(CTL) list $(if $(STATUS),--status $(STATUS))

show: ## Show a proposal in full: make show ID=<proposal-id>
	$(call need-id,proposal-id)
	$(CTL) show $(ID)

approve: ## Approve + canary rollout (interactive): make approve ID=<proposal-id>
	$(call need-id,proposal-id)
	$(CTL) approve $(ID)

reject: ## Reject a proposal: make reject ID=<proposal-id> NOTE="why"
	$(call need-id,proposal-id)
	$(CTL) reject $(ID) --note "$(NOTE)"

rollback: ## Roll back an applied rollout: make rollback ID=<rollout-id>
	$(call need-id,rollout-id)
	$(CTL) rollback $(ID)

audit: ## The full audit trail
	$(CTL) audit

fleet: ## Fleet state: agents, unhealthy, series by service
	$(CTL) fleet
