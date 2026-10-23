PY := .venv/bin/python
WORKERS ?= 8

.PHONY: setup test test-fast chaos chaos-smoke chaos-real bench report demo clean-runs

setup:
	uv venv --python 3.12 .venv
	uv pip install -e . pytest hypothesis

test:
	$(PY) -m pytest

test-fast:
	$(PY) -m pytest tests/unit tests/integration

## Full fake-model chaos suite: 1,000 runs each for AW, B1, B0 plus the power-loss,
## torn-write and receiver-assumption suites. Then regenerate the report.
chaos:
	$(PY) -m chaos.harness --config all --workers $(WORKERS)
	$(PY) -m chaos.report

chaos-smoke:
	$(PY) -m chaos.harness --config smoke --workers $(WORKERS)

## 30 real-model runs with a free local LLM (Ollama, qwen3:8b). Pilot first, then the suite.
chaos-real:
	$(PY) -m chaos.real

bench:
	$(PY) -m chaos.bench

report:
	$(PY) -m chaos.report

## Kill-and-resume demo for the terminal recording.
demo:
	bash demo/kill_resume_demo.sh

clean-runs:
	rm -rf runs
