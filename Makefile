# Makefile for lmer (Oracle Linux 9 Slim FIPS container)
# Usage: make [target]

IMAGE_NAME := lmer
FULL_IMAGE := $(IMAGE_NAME)

# Detect container runtime (Docker or Podman). LMER_CONTAINER_RUNTIME overrides,
# with lmer_cli.runtime.detect_runtime()'s rule: trimmed, case-insensitive,
# docker or podman only. Only the letters of those two names are lowercased;
# any other letter fails the filter either way.
lower_runtime = $(subst D,d,$(subst O,o,$(subst C,c,$(subst K,k,$(subst E,e,$(subst R,r,$(subst P,p,$(subst M,m,$(subst A,a,$(subst N,n,$(1)))))))))))
RUNTIME_OVERRIDE := $(strip $(LMER_CONTAINER_RUNTIME))
ifneq ($(RUNTIME_OVERRIDE),)
RUNTIME_NAME := $(if $(filter 1,$(words $(RUNTIME_OVERRIDE))),$(filter docker podman,$(call lower_runtime,$(RUNTIME_OVERRIDE))))
ifeq ($(RUNTIME_NAME),)
CONTAINER_CMD :=
CONTAINER_ERROR := LMER_CONTAINER_RUNTIME=$(RUNTIME_OVERRIDE) is not supported; use docker or podman
else
CONTAINER_CMD := $(shell command -v $(RUNTIME_NAME) 2>/dev/null)
CONTAINER_ERROR := LMER_CONTAINER_RUNTIME=$(RUNTIME_NAME) but $(RUNTIME_NAME) is not on PATH
endif
else
CONTAINER_CMD := $(shell command -v docker 2>/dev/null || command -v podman 2>/dev/null)
CONTAINER_ERROR := Neither docker nor podman found. Please install Docker or Podman to continue
endif
# Expanded when a recipe runs, so only targets that use the runtime fail.
REQUIRE_RUNTIME = $(if $(CONTAINER_CMD),,$(error $(CONTAINER_ERROR)))

# Platform detection for multi-arch support
PLATFORM :=
ifeq ($(shell uname -m),arm64)
	PLATFORM := --platform linux/amd64
endif

# Colors for output
RED := \033[0;31m
GREEN := \033[0;32m
YELLOW := \033[0;33m
NC := \033[0m # No Color

.DEFAULT_GOAL := help

.PHONY: help build build-nocache lmer lmer-shell claude claude-shell \
        test test-all fips-check security-scan dep-up rebuild info version

## Help
help: ## Show this help message
	@echo "$(GREEN)lmer container management$(NC)"
	@echo "========================="
	@echo ""
	@echo "Available targets:"
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  $(YELLOW)%-18s$(NC) %s\n", $$1, $$2}'
	@echo ""
	@echo "Examples:"
	@echo "  make build        # Build the container image"
	@echo "  make lmer         # Run lmer with Claude in a fresh container"
	@echo "  make test         # Run the test suite locally"
	@echo ""

## Run lmer
lmer: build ## Build and run lmer with Claude (resource-limited)
	@echo "$(GREEN)🤖 Starting lmer with Claude...$(NC)"
	@echo "$(YELLOW)Resource limits: 1 CPU, 2GB RAM, 512 process limit$(NC)"
	@./lmer

lmer-shell: build ## Get a shell in the lmer container
	@echo "$(GREEN)🐚 Entering lmer container shell...$(NC)"
	@echo "$(YELLOW)Resource limits active: 1 CPU, 2GB RAM$(NC)"
	@./lmer --no-clone --exec bash

## Backwards-compatibility aliases
claude: lmer ## Alias for lmer
claude-shell: lmer-shell ## Alias for lmer-shell

## Build
build: ## Build the container image
	$(REQUIRE_RUNTIME)
	@echo "$(GREEN)🔨 Building lmer container...$(NC)"
	@echo "📦 Using container command: $(CONTAINER_CMD)"
	@BUILD_ARGS=""; \
	if [ -z "$$BUILD_UID" ]; then BUILD_UID=$$(id -u); fi; \
	if [ -z "$$BUILD_GID" ]; then BUILD_GID=$$(id -g); fi; \
	if [ -n "$$BUILD_UID" ] && [ -n "$$BUILD_GID" ]; then \
		BUILD_ARGS="--build-arg BUILD_UID=$$BUILD_UID --build-arg BUILD_GID=$$BUILD_GID"; \
		echo "🔧 Building with user ID: UID=$$BUILD_UID GID=$$BUILD_GID"; \
	fi; \
	CACHE_FROM=""; \
	if echo "$(CONTAINER_CMD)" | grep -q docker; then \
		CACHE_FROM="--cache-from $(FULL_IMAGE)"; \
	fi; \
	$(CONTAINER_CMD) build $(PLATFORM) \
		$$CACHE_FROM \
		$$BUILD_ARGS \
		-t $(FULL_IMAGE) \
		-f Containerfile .
	@echo "$(GREEN)✅ Container built successfully!$(NC)"

build-nocache: ## Build the container image without cache
	$(REQUIRE_RUNTIME)
	@echo "$(GREEN)🔨 Building container (no cache)...$(NC)"
	@BUILD_ARGS=""; \
	if [ -z "$$BUILD_UID" ]; then BUILD_UID=$$(id -u); fi; \
	if [ -z "$$BUILD_GID" ]; then BUILD_GID=$$(id -g); fi; \
	if [ -n "$$BUILD_UID" ] && [ -n "$$BUILD_GID" ]; then \
		BUILD_ARGS="--build-arg BUILD_UID=$$BUILD_UID --build-arg BUILD_GID=$$BUILD_GID"; \
		echo "🔧 Building with user ID: UID=$$BUILD_UID GID=$$BUILD_GID"; \
	fi; \
	$(CONTAINER_CMD) build $(PLATFORM) \
		--no-cache \
		$$BUILD_ARGS \
		-t $(FULL_IMAGE) \
		-f Containerfile .
	@echo "$(GREEN)✅ Container built successfully!$(NC)"

## Testing
test: ## Run quick tests locally (excludes container build tests)
	@echo "$(GREEN)🧪 Running quick tests locally...$(NC)"
	@if [ -f .venv/bin/activate ]; then \
		. .venv/bin/activate && python -m pytest tests/ -v --ignore=tests/test_container_build.py; \
	else \
		python -m pytest tests/ -v --ignore=tests/test_container_build.py; \
	fi

test-all: ## Run all tests including container build tests
	@echo "$(GREEN)🧪 Running all tests locally...$(NC)"
	@if [ -f .venv/bin/activate ]; then \
		. .venv/bin/activate && python -m pytest tests/ -v; \
	else \
		python -m pytest tests/ -v; \
	fi

## Security and compliance
fips-check: ## Verify FIPS mode in container
	$(REQUIRE_RUNTIME)
	@echo "$(GREEN)🔒 Checking FIPS status...$(NC)"
	@$(CONTAINER_CMD) run --rm $(FULL_IMAGE) bash -c " \
		echo '🔒 FIPS Kernel Status:' && \
		cat /proc/sys/crypto/fips_enabled 2>/dev/null || echo 'N/A' && \
		echo '' && \
		echo '🔐 OpenSSL Version:' && \
		openssl version && \
		echo '' && \
		echo '🐍 Python Crypto Test:' && \
		python3 -c 'import hashlib; print(\"SHA-256: OK\"); hashlib.sha256(b\"test\").hexdigest()' \
	"

# The message carries the raw override value, so it reaches the shell through
# the environment rather than being pasted into the recipe text.
security-scan: export CONTAINER_ERROR := $(CONTAINER_ERROR)
security-scan: ## Run security scan on image
	@echo "$(GREEN)🔍 Running security scan...$(NC)"
	@if command -v trivy >/dev/null 2>&1; then \
		trivy image --severity HIGH,CRITICAL $(FULL_IMAGE); \
	else \
		echo "$(YELLOW)⚠️  Trivy not installed. Install with: brew install trivy$(NC)"; \
		echo "Attempting to use container-based scanner..."; \
		[ -n "$(CONTAINER_CMD)" ] || { echo "$(RED)❌ $$CONTAINER_ERROR$(NC)" >&2; exit 1; }; \
		$(CONTAINER_CMD) run --rm \
			-v /var/run/docker.sock:/var/run/docker.sock \
			aquasec/trivy image --severity HIGH,CRITICAL $(FULL_IMAGE); \
	fi

## Maintenance
dep-up: ## Upgrade every locked dependency to the newest version its declared range allows
	@echo "$(GREEN)⬆️  Upgrading Python lockfile (uv.lock)...$(NC)"
	@uv lock --upgrade
	@echo "$(GREEN)⬆️  Upgrading web lockfile (web/package-lock.json)...$(NC)"
	@cd web && npm update --package-lock-only
	@echo "$(GREEN)✅ Lockfiles upgraded. Review the diff, run the gate, commit.$(NC)"

rebuild: build-nocache ## Full rebuild from scratch (no cache)
	@echo "$(GREEN)✅ Full rebuild complete$(NC)"

## Information
info: ## Show environment information
	$(REQUIRE_RUNTIME)
	@echo "$(GREEN)📋 Environment Information$(NC)"
	@echo "=========================="
	@echo "Container Runtime: $(CONTAINER_CMD)"
	@echo "Image: $(FULL_IMAGE)"
	@echo "Platform: $(shell uname -m)"
	@if [ -n "$(PLATFORM)" ]; then \
		echo "Build Platform Override: $(PLATFORM)"; \
	fi
	@echo ""
	@echo "$(GREEN)📦 Container Runtime Version:$(NC)"
	@$(CONTAINER_CMD) version --format '{{.Client.Version}}' 2>/dev/null || $(CONTAINER_CMD) --version

version: ## Show Makefile version
	@echo "Makefile for lmer container"
	@echo "Version: 1.0.0"
	@echo "Last Updated: $$(date)"
