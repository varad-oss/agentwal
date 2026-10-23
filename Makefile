PY := .venv/bin/python
WORKERS ?= 8

.PHONY: setup test test-fast chaos chaos-smoke chaos-real bench report demo gif clean-runs

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

## Re-record docs/demo.gif (needs: brew install asciinema agg)
gif:
	DEMO_PAUSE=1.5 DEMO_COLS=104 asciinema rec --headless --overwrite --window-size 104x30 -i 2 \
		-c "bash demo/kill_resume_demo.sh" docs/demo.cast
	agg --font-size 15 --theme monokai --idle-time-limit 2 --last-frame-duration 6 docs/demo.cast docs/demo.gif

clean-runs:
	rm -rf runs
