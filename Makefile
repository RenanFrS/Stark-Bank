.PHONY: install install-dev keys smoke webhook-list webhook-register run test coverage docker-build

install:
	pip install -r requirements.txt

install-dev:
	pip install -r requirements-dev.txt

keys:
	python scripts/generate_keys.py

smoke:
	python scripts/smoke_test.py

webhook-list:
	python scripts/manage_webhook.py list

# usage: make webhook-register URL=https://your-app.fly.dev
webhook-register:
	python scripts/manage_webhook.py register $(URL)

run:
	uvicorn app.main:app --reload --port 8080

test:
	pytest

coverage:
	pytest --cov=app --cov-report=term-missing

docker-build:
	docker build -t starkbank-challenge .
