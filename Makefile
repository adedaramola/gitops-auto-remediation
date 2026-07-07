.DEFAULT_GOAL := help
LAMBDAS_DIR  := lambdas
TF_DIR       := terraform
PYTHON       := python3

# ── Help ──────────────────────────────────────────────────────────────────────
.PHONY: help
help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── Python environment ────────────────────────────────────────────────────────
.PHONY: install
install: ## Install runtime + dev dependencies
	pip install -r $(LAMBDAS_DIR)/requirements-dev.txt

# ── Tests ─────────────────────────────────────────────────────────────────────
.PHONY: test
test: ## Run all unit tests
	cd $(LAMBDAS_DIR) && $(PYTHON) -m pytest tests/ -v

.PHONY: test-cov
test-cov: ## Run tests with coverage report
	cd $(LAMBDAS_DIR) && $(PYTHON) -m pytest tests/ -v \
		--cov=signal_collector --cov=decision_engine --cov=outcome_validator \
		--cov=classifier_agent --cov=root_cause_agent \
		--cov=action_planner --cov=confidence_scorer \
		--cov-report=term-missing --cov-report=html:../coverage

# ── Lint ──────────────────────────────────────────────────────────────────────
.PHONY: lint
lint: ## Lint Lambda source with ruff (pip install ruff)
	ruff check $(LAMBDAS_DIR)/signal_collector/app.py \
	           $(LAMBDAS_DIR)/decision_engine/app.py \
	           $(LAMBDAS_DIR)/outcome_validator/app.py \
	           $(LAMBDAS_DIR)/classifier_agent/app.py \
	           $(LAMBDAS_DIR)/root_cause_agent/app.py \
	           $(LAMBDAS_DIR)/action_planner/app.py \
	           $(LAMBDAS_DIR)/confidence_scorer/app.py

.PHONY: fmt
fmt: ## Auto-format Lambda source with ruff
	ruff format $(LAMBDAS_DIR)/signal_collector/app.py \
	            $(LAMBDAS_DIR)/decision_engine/app.py \
	            $(LAMBDAS_DIR)/outcome_validator/app.py \
	            $(LAMBDAS_DIR)/classifier_agent/app.py \
	            $(LAMBDAS_DIR)/root_cause_agent/app.py \
	            $(LAMBDAS_DIR)/action_planner/app.py \
	            $(LAMBDAS_DIR)/confidence_scorer/app.py

# ── Terraform ─────────────────────────────────────────────────────────────────
.PHONY: tf-init
tf-init: ## terraform init
	cd $(TF_DIR) && terraform init

.PHONY: tf-plan
tf-plan: ## terraform plan (requires terraform.tfvars)
	cd $(TF_DIR) && terraform plan -out=tfplan

.PHONY: tf-apply
tf-apply: ## terraform apply previously saved plan
	cd $(TF_DIR) && terraform apply tfplan

.PHONY: tf-destroy
tf-destroy: ## terraform destroy (prompts for confirmation)
	cd $(TF_DIR) && terraform destroy

.PHONY: tf-validate
tf-validate: ## Validate Terraform configuration
	cd $(TF_DIR) && terraform validate

# ── Demo helpers ──────────────────────────────────────────────────────────────
.PHONY: demo-alert
demo-alert: ## POST docs/demo-alert.json to $$WEBHOOK_URL (optional $$WEBHOOK_SECRET)
	@test -n "$$WEBHOOK_URL" || (echo "Set WEBHOOK_URL to your Terraform output first." && exit 1)
	@if [ -n "$$WEBHOOK_SECRET" ]; then \
		curl -sS -X POST "$$WEBHOOK_URL" \
			-H "Content-Type: application/json" \
			-H "X-Webhook-Secret: $$WEBHOOK_SECRET" \
			--data @docs/demo-alert.json; \
	else \
		curl -sS -X POST "$$WEBHOOK_URL" \
			-H "Content-Type: application/json" \
			--data @docs/demo-alert.json; \
	fi

.PHONY: demo-preflight
demo-preflight: ## Verify Argo sync, demo-service readiness, and model access before a live demo
	bash scripts/demo_preflight.sh

# ── Lambda sync ───────────────────────────────────────────────────────────────
.PHONY: sync-lambda
sync-lambda: ## Copy lambdas/*/app.py → terraform/modules/lambda_*/src/app.py
	@for fn in signal_collector decision_engine outcome_validator classifier_agent root_cause_agent action_planner confidence_scorer; do \
		cp $(LAMBDAS_DIR)/$$fn/app.py $(TF_DIR)/modules/lambda_$$fn/src/app.py && \
		echo "  synced $$fn"; \
	done

.PHONY: check-lambda-sync
check-lambda-sync: sync-lambda ## Fail if lambdas/ and terraform module sources are out of sync
	@git diff --exit-code terraform/modules/lambda_*/src/app.py || \
		(echo "\nERROR: Lambda sources are out of sync. Run 'make sync-lambda' and commit." && exit 1)

# ── Lambda packaging ──────────────────────────────────────────────────────────
.PHONY: package
package: ## Zip each Lambda function for manual deployment
	@for fn in signal_collector decision_engine outcome_validator classifier_agent root_cause_agent action_planner confidence_scorer; do \
		echo "Packaging $$fn..."; \
		cd $(LAMBDAS_DIR)/$$fn && \
		pip install -r ../requirements.txt -t ./package --quiet && \
		cp app.py ./package/ && \
		cd package && zip -qr ../../$$fn.zip . && \
		cd .. && rm -rf package && cd ../..; \
	done
	@echo "Zip files written to lambdas/"

# ── CI shortcut ───────────────────────────────────────────────────────────────
.PHONY: ci
ci: lint test ## Run lint + tests (used in CI)
