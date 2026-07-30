PYTHON ?= python3.10
DATA_DIR ?= /tmp/agentenv-py-demo

.PHONY: demo test serve clean

demo:
	PYTHONPATH=src $(PYTHON) -m agentenv --data-dir $(DATA_DIR) demo

test:
	PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

serve:
	PYTHONPATH=src $(PYTHON) -m agentenv --data-dir $(DATA_DIR) serve

clean:
	find src tests -type d -name __pycache__ -prune -exec rm -r {} +
