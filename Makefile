PYTHON ?= python3.10
DATA_DIR ?= /tmp/agentenv-py-demo
BACKEND ?= local

.PHONY: demo docker-demo test serve clean

demo:
	PYTHONPATH=src $(PYTHON) -m agentenv --backend $(BACKEND) --data-dir $(DATA_DIR) demo

docker-demo:
	PYTHONPATH=src $(PYTHON) -m agentenv --backend docker --data-dir /tmp/agentenv-docker-demo demo

test:
	PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

serve:
	PYTHONPATH=src $(PYTHON) -m agentenv --backend $(BACKEND) --data-dir $(DATA_DIR) serve

clean:
	find src tests examples -type d -name __pycache__ -prune -exec rm -r {} +
