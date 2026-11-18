# RouteRTL Project Makefile

# SDK root resolved from pip-installed routertl package
SDK_ROOT := $(shell rr sdk-path)
ROOT_DIR := $(shell pwd)
-include $(SDK_ROOT)/Common.mk

# Fallback if SDK not found
all:
	@echo "RouteRTL SDK not found. Is routertl installed? (pip install routertl)"

# Documentation link checker
.PHONY: doc-lint
doc-lint:
	@python3 tools/doc-lint/check_links.py

# Public export gate and staging (DS-P2.113, DS-P2.116)
.PHONY: export-gate export github-export
export-gate:
	python3 tools/public_artifact_hygiene.py --export-gate

export:
	python3 tools/public_artifact_hygiene.py --export-to $(or $(DEST),dist/export)

github-export:
	python3 tools/export_github.py --dest $(or $(DEST),dist/github_export)
