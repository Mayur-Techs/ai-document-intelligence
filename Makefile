.PHONY: help up down build migrate migrate-new psql test test-coverage health stats docs export upload-test lint setup

BOLD  := \033[1m
RESET := \033[0m
CYAN  := \033[36m
GREEN := \033[32m

help:
	@echo ""
	@echo "$(BOLD)AI Document Intelligence - Developer Commands$(RESET)"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  $(CYAN)%-18s$(RESET) %s\n", $$1, $$2}'
	@echo ""

up: ## Start API + PostgreSQL + n8n (ports 8001, 5433, 5679)
	docker compose up --build -d
	@echo "$(GREEN)Stack started$(RESET)"
	@echo "  API:  http://localhost:8001/docs"
	@echo "  n8n:  http://localhost:5679"

down: ## Stop all services
	docker compose down

build: ## Rebuild Docker images
	docker compose build --no-cache

migrate: ## Apply Alembic migrations
	docker compose exec api alembic upgrade head

migrate-new: ## Generate migration (usage: make migrate-new MSG="description")
	@if [ -z "$(MSG)" ]; then echo "Usage: make migrate-new MSG='your message'"; exit 1; fi
	docker compose exec api alembic revision --autogenerate -m "$(MSG)"

psql: ## Open PostgreSQL shell
	docker compose exec db psql -U docai -d docai

test: ## Run all tests (no Docker required)
	python -m pytest tests/ -v --tb=short

test-coverage: ## Tests with coverage report
	python -m pytest tests/ --cov=. --cov-report=term-missing

health: ## Check API health
	curl -s "http://localhost:8001/health" | python3 -m json.tool

stats: ## Document processing stats
	curl -s "http://localhost:8001/api/v1/documents/stats/summary" | python3 -m json.tool

docs: ## List all documents
	curl -s "http://localhost:8001/api/v1/documents/" | python3 -m json.tool

export: ## Export document summaries as CSV
	curl -s "http://localhost:8001/api/v1/documents/export?status=completed" -o docs_export_$(shell date +%Y%m%d).csv
	@echo "$(GREEN)Saved to docs_export_$(shell date +%Y%m%d).csv$(RESET)"

upload-test: ## Upload a test PDF (usage: make upload-test FILE=path/to/invoice.pdf)
	@if [ -z "$(FILE)" ]; then echo "Usage: make upload-test FILE=invoice.pdf"; exit 1; fi
	curl -s -X POST "http://localhost:8001/api/v1/documents/upload" \
		-F "file=@$(FILE)" \
		-F "document_type=invoice" | python3 -m json.tool

lint: ## Lint with ruff
	ruff check .

setup: ## First-time setup
	@if [ ! -f .env ]; then cp .env.example .env; echo "$(GREEN)Created .env$(RESET)"; fi
	pip install -r requirements.txt
	@echo "Next: edit .env -> add CEREBRAS_API_KEY/GROQ_API_KEY -> make up -> make migrate"
