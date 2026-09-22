SHELL=/bin/bash -o pipefail

#
# ERE Makefile: Developer-friendly interface for testing & quality assurance
#
# This Makefile provides quick, discoverable targets for common development tasks.
# It uses your active Poetry environment for fast feedback during development.
#
# For CI/CD: Use `tox` (see tox.ini) for reproducible, isolated test environments.
# tox is independent of Poetry and manages its own dependencies in CI.
#
# Three-environment model (Cosmic Python / Clean Code):
#   make test-unit              → pytest + coverage (your venv, fast)
#   make lint                   → pylint checks (your venv, fast)
#   make check-clean-code       → tox isolated: pylint + radon + xenon
#   make check-architecture     → tox isolated: import-linter
#   make all-quality-checks     → full pipeline: lint + architecture + clean-code
#
# For CI/CD in GitHub Actions:
#   tox -e py312,architecture,clean-code
#

BUILD_PRINT = \e[1;34m
END_BUILD_PRINT = \e[0m

PROJECT_PATH = $(shell pwd)
SRC_PATH = ${PROJECT_PATH}/src
TEST_PATH = ${PROJECT_PATH}/test
BUILD_PATH = ${PROJECT_PATH}/dist
INFRA_PATH = ${PROJECT_PATH}/src/infra
COMPOSE_FILE = ${INFRA_PATH}/compose.dev.yaml
ENV_FILE = ${INFRA_PATH}/.env

# Auto-export all .env variables to every recipe shell (if the file exists)
ifneq ($(wildcard $(ENV_FILE)),)
include $(ENV_FILE)
ENV_VARS := $(shell sed -n '/^[[:space:]]*\#/d; /^[[:space:]]*$$/d; s/^[[:space:]]*\([A-Za-z_][A-Za-z0-9_]*\)[[:space:]]*=.*/\1/p' "$(ENV_FILE)")
export $(ENV_VARS)
endif

PACKAGE_NAME = ere

ICON_DONE = [✔]
ICON_ERROR = [x]
ICON_WARNING = [!]
ICON_PROGRESS = [-]

#-----------------------------------------------------------------------------
# Dev commands
#-----------------------------------------------------------------------------
.PHONY: help install-poetry install build
help: ## Display available targets
	@ echo -e "$(BUILD_PRINT)Available targets:$(END_BUILD_PRINT)"
	@ echo ""
	@ echo -e "  $(BUILD_PRINT)Development:$(END_BUILD_PRINT)"
	@ echo "    install              - Install project dependencies via Poetry"
	@ echo "    install-poetry       - Install Poetry if not present"
	@ echo "    build                - Build the package distribution"
	@ echo ""
	@ echo -e "  $(BUILD_PRINT)Testing:$(END_BUILD_PRINT)"
	@ echo "    test                 - Run all tests"
	@ echo "    test-unit            - Run unit tests with coverage (fast, your venv)"
	@ echo "    test-integration     - Run integration tests only"
	@ echo "    test-coverage        - Generate HTML coverage report"
	@ echo ""
	@ echo -e "  $(BUILD_PRINT)Code Quality (Developer):$(END_BUILD_PRINT)"
	@ echo "    format               - Format code with Ruff"
	@ echo "    lint                 - Run pylint checks (your venv, fast)"
	@ echo "    lint-fix             - Auto-fix with Ruff"
	@ echo ""
	@ echo -e "  $(BUILD_PRINT)Code Quality (CI/Isolated):$(END_BUILD_PRINT)"
	@ echo "    check-clean-code     - Clean-code checks: pylint + radon + xenon (tox)"
	@ echo "    check-architecture   - Validate layer contracts (tox)"
	@ echo "    all-quality-checks   - Run all quality checks"
	@ echo "    ci                   - Full CI pipeline for GitHub Actions"
	@ echo ""
	@ echo -e "  $(BUILD_PRINT)Infrastructure (Docker):$(END_BUILD_PRINT)"
	@ echo "    infra-build          - Build the ERE Docker image"
	@ echo "    infra-up             - Start services (docker compose up -d)"
	@ echo "    infra-down           - Stop and remove stack containers and networks"
	@ echo "    infra-down-volumes   - Stop services and remove volumes (clean slate)"
	@ echo "    infra-rebuild        - Rebuild images and start services"
	@ echo "    infra-rebuild-clean  - Rebuild from scratch (no cache) and start"
	@ echo "    infra-logs           - Follow service logs"
	@ echo "    infra-watch          - Start services with file watching (sync src/ and src/config/)"
	@ echo ""
	@ echo -e "  $(BUILD_PRINT)Utilities:$(END_BUILD_PRINT)"
	@ echo "    clean                - Remove build artifacts and caches"
	@ echo "    help                 - Display this help message"
	@ echo ""

install-poetry: ## Install Poetry if not present
	@ echo -e "$(BUILD_PRINT)$(ICON_PROGRESS) Installing Poetry $(END_BUILD_PRINT)"
	@ pip install "poetry>=2.0.0"
	@ echo -e "$(BUILD_PRINT)$(ICON_DONE) Poetry is installed$(END_BUILD_PRINT)"

install: install-poetry ## Install project dependencies
	@ echo -e "$(BUILD_PRINT)$(ICON_PROGRESS) Installing ERE requirements$(END_BUILD_PRINT)"
	@ cd src && poetry lock
	@ cd src && poetry install --with dev
	@ echo -e "$(BUILD_PRINT)$(ICON_DONE) ERE requirements are installed$(END_BUILD_PRINT)"

build: ## Build the package distribution
	@ echo -e "$(BUILD_PRINT)$(ICON_PROGRESS) Building package$(END_BUILD_PRINT)"
	@ cd src && poetry build
	@ echo -e "$(BUILD_PRINT)$(ICON_DONE) Package built successfully$(END_BUILD_PRINT)"

#-----------------------------------------------------------------------------
# Testing commands
#-----------------------------------------------------------------------------
.PHONY: test test-unit test-integration test-coverage
test: ## Run all tests
	@ echo -e "$(BUILD_PRINT)$(ICON_PROGRESS) Running all tests$(END_BUILD_PRINT)"
	@ cd src && poetry run pytest --rootdir=$(SRC_PATH) $(TEST_PATH)
	@ echo -e "$(BUILD_PRINT)$(ICON_DONE) All tests passed$(END_BUILD_PRINT)"

test-unit: ## Run unit tests with coverage (fast, uses your venv)
	@ echo -e "$(BUILD_PRINT)$(ICON_PROGRESS) Running unit tests with coverage$(END_BUILD_PRINT)"
	@ cd src && poetry run pytest --rootdir=$(SRC_PATH) $(TEST_PATH) -m "not integration" \
	    --cov=ere --cov-report=term-missing --cov-report=html:htmlcov
	@ echo -e "$(BUILD_PRINT)$(ICON_DONE) Unit tests passed (coverage: htmlcov/index.html)$(END_BUILD_PRINT)"

test-integration: check-env ## Run integration tests only (requires Redis — run make infra-up first)
	@ echo -e "$(BUILD_PRINT)$(ICON_PROGRESS) Running integration tests$(END_BUILD_PRINT)"
	@ cd src && poetry run pytest --rootdir=$(SRC_PATH) $(TEST_PATH) -m "integration"
	@ echo -e "$(BUILD_PRINT)$(ICON_DONE) Integration tests passed$(END_BUILD_PRINT)"

test-coverage: ## Generate detailed HTML coverage report
	@ echo -e "$(BUILD_PRINT)$(ICON_PROGRESS) Generating coverage report$(END_BUILD_PRINT)"
	@ cd src && poetry run pytest --rootdir=$(SRC_PATH) $(TEST_PATH) -m "not integration" \
	    --cov=ere --cov-report=html:htmlcov --cov-report=term-missing
	@ echo -e "$(BUILD_PRINT)$(ICON_DONE) Coverage report: htmlcov/index.html$(END_BUILD_PRINT)"

#-----------------------------------------------------------------------------
# Code quality commands
#-----------------------------------------------------------------------------
.PHONY: format lint lint-fix check-clean-code check-architecture check-specs all-quality-checks ci

format: ## Format code with Ruff
	@ echo -e "$(BUILD_PRINT)$(ICON_PROGRESS) Formatting code$(END_BUILD_PRINT)"
	@ cd src && poetry run ruff format $(SRC_PATH) $(TEST_PATH)
	@ echo -e "$(BUILD_PRINT)$(ICON_DONE) Format complete$(END_BUILD_PRINT)"

lint: ## Run pylint checks (style, naming, SOLID principles) — uses your venv
	@ echo -e "$(BUILD_PRINT)$(ICON_PROGRESS) Running pylint checks$(END_BUILD_PRINT)"
	@ cd src && poetry run pylint --rcfile=$(PROJECT_PATH)/.pylintrc $(SRC_PATH)/ere $(TEST_PATH)
	@ echo -e "$(BUILD_PRINT)$(ICON_DONE) Pylint checks passed$(END_BUILD_PRINT)"

lint-fix: ## Auto-fix code style with Ruff
	@ echo -e "$(BUILD_PRINT)$(ICON_PROGRESS) Auto-fixing with Ruff$(END_BUILD_PRINT)"
	@ cd src && poetry run ruff check --fix $(SRC_PATH) $(TEST_PATH)
	@ echo -e "$(BUILD_PRINT)$(ICON_DONE) Auto-fix complete$(END_BUILD_PRINT)"

check-clean-code: ## Clean-code checks: pylint + radon + xenon (isolated tox)
	@ echo -e "$(BUILD_PRINT)$(ICON_PROGRESS) Running clean-code checks (tox isolated)$(END_BUILD_PRINT)"
	@ cd src && poetry run tox -e clean-code
	@ echo -e "$(BUILD_PRINT)$(ICON_DONE) Clean-code checks passed$(END_BUILD_PRINT)"

check-architecture: ## Validate architectural boundaries (isolated tox)
	@ echo -e "$(BUILD_PRINT)$(ICON_PROGRESS) Checking architecture contracts (tox isolated)$(END_BUILD_PRINT)"
	@ cd src && poetry run tox -e architecture
	@ echo -e "$(BUILD_PRINT)$(ICON_DONE) Architecture checks passed$(END_BUILD_PRINT)"

check-specs: ## Validate OpenSpec artifacts (structural, strict; pinned OpenSpec 1.4.1)
	@ echo -e "$(BUILD_PRINT)$(ICON_PROGRESS) Validating OpenSpec specs and changes$(END_BUILD_PRINT)"
	@ npx -y @fission-ai/openspec@1.4.1 validate --all --strict --no-interactive
	@ echo -e "$(BUILD_PRINT)$(ICON_DONE) OpenSpec validation passed$(END_BUILD_PRINT)"

all-quality-checks: lint check-clean-code check-architecture check-specs ## Run all: lint + clean-code + architecture + specs
	@ echo -e "$(BUILD_PRINT)$(ICON_DONE) All quality checks passed!$(END_BUILD_PRINT)"

ci: ## Full CI pipeline for GitHub Actions (tox)
	@ echo -e "$(BUILD_PRINT)$(ICON_PROGRESS) Running full CI pipeline$(END_BUILD_PRINT)"
	@ set -a && . $(ENV_FILE) && set +a && poetry -C ./src run tox -e py312,architecture,clean-code
	@ echo -e "$(BUILD_PRINT)$(ICON_DONE) CI pipeline complete$(END_BUILD_PRINT)"

#-----------------------------------------------------------------------------
# Infrastructure commands (Docker)
#-----------------------------------------------------------------------------
.PHONY: check-env infra-build infra-up infra-down infra-down-volumes infra-rebuild infra-rebuild-clean infra-logs infra-watch

check-env:
	@ test -f $(ENV_FILE) || (echo -e "$(BUILD_PRINT)$(ICON_ERROR) Missing $(ENV_FILE). Run: cp infra/.env.example infra/.env$(END_BUILD_PRINT)" && exit 1)

infra-build: check-env ## Build the ERE Docker image
	@ echo -e "$(BUILD_PRINT)$(ICON_PROGRESS) Building ERE Docker image$(END_BUILD_PRINT)"
	@ docker compose -f $(COMPOSE_FILE) --env-file $(ENV_FILE) build
	@ echo -e "$(BUILD_PRINT)$(ICON_DONE) ERE image built$(END_BUILD_PRINT)"

infra-up: check-env ## Start services (docker compose up -d)
	@ docker network create ersys-local || true
	@ echo -e "$(BUILD_PRINT)$(ICON_PROGRESS) Starting ERE stack$(END_BUILD_PRINT)"
	@ docker compose -f $(COMPOSE_FILE) --env-file $(ENV_FILE) up -d
	@ echo -e "$(BUILD_PRINT)$(ICON_DONE) ERE stack is running — use 'make infra-logs' to follow output$(END_BUILD_PRINT)"

infra-down: check-env ## Stop and remove ERE stack containers and networks
	@ echo -e "$(BUILD_PRINT)$(ICON_PROGRESS) Stopping ERE stack$(END_BUILD_PRINT)"
	@ docker compose -f $(COMPOSE_FILE) --env-file $(ENV_FILE) down
	@ echo -e "$(BUILD_PRINT)$(ICON_DONE) ERE stack stopped$(END_BUILD_PRINT)"

infra-down-volumes: check-env ## Stop services and remove volumes (clean slate)
	@ echo -e "$(BUILD_PRINT)$(ICON_PROGRESS) Stopping ERE stack and removing volumes$(END_BUILD_PRINT)"
	@ docker compose -f $(COMPOSE_FILE) --env-file $(ENV_FILE) down -v
	@ echo -e "$(BUILD_PRINT)$(ICON_DONE) ERE stack stopped and volumes removed$(END_BUILD_PRINT)"

infra-rebuild: check-env ## Rebuild images and start services
	@ echo -e "$(BUILD_PRINT)$(ICON_PROGRESS) Rebuilding ERE stack$(END_BUILD_PRINT)"
	@ docker compose -f $(COMPOSE_FILE) --env-file $(ENV_FILE) up -d --build
	@ echo -e "$(BUILD_PRINT)$(ICON_DONE) ERE stack rebuilt and started$(END_BUILD_PRINT)"

infra-rebuild-clean: check-env ## Rebuild from scratch (no cache) and start
	@ echo -e "$(BUILD_PRINT)$(ICON_PROGRESS) Rebuilding ERE stack (no cache)$(END_BUILD_PRINT)"
	@ docker compose -f $(COMPOSE_FILE) --env-file $(ENV_FILE) build --no-cache
	@ docker compose -f $(COMPOSE_FILE) --env-file $(ENV_FILE) up -d
	@ echo -e "$(BUILD_PRINT)$(ICON_DONE) ERE stack rebuilt (clean) and started$(END_BUILD_PRINT)"

infra-logs: check-env ## Follow service logs
	@ docker compose -f $(COMPOSE_FILE) --env-file $(ENV_FILE) logs -f

infra-watch: check-env ## Start services with file watching (sync src/ and src/config/)
	@ echo -e "$(BUILD_PRINT)$(ICON_PROGRESS) Starting ERE stack with watch$(END_BUILD_PRINT)"
	@ docker compose -f $(COMPOSE_FILE) --env-file $(ENV_FILE) watch

#-----------------------------------------------------------------------------
# Utility commands
#-----------------------------------------------------------------------------
.PHONY: clean
clean: ## Remove build artifacts and caches
	@ echo -e "$(BUILD_PRINT)$(ICON_PROGRESS) Cleaning build artifacts and caches$(END_BUILD_PRINT)"
	@ rm -rf $(BUILD_PATH)
	@ rm -rf .pytest_cache
	@ rm -rf .tox
	@ rm -rf *.egg-info
	@ rm -rf src/*.egg-info
	@ rm -rf htmlcov coverage.xml
	@ cd src && poetry run ruff clean
	@ find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@ find . -type f -name "*.pyc" -delete 2>/dev/null || true
	@ find . -type f -name "*.pyo" -delete 2>/dev/null || true
	@ echo -e "$(BUILD_PRINT)$(ICON_DONE) Clean complete$(END_BUILD_PRINT)"

# Default target
.DEFAULT_GOAL := help
