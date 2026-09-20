.PHONY: help install install-dev test test-cov lint format build deploy destroy \
        dry-run scan-stack setup-ssm logs tail-logs validate clean invoke

STACK_NAME    ?= cfn-drift-fixer
REGION        ?= us-east-1
ENVIRONMENT   ?= prod
TARGET_STACK  ?= my-stack-name

help:
	@echo ""
	@echo "CFN Drift Fixer — Commands"
	@echo "──────────────────────────────────────────────"
	@echo "  make install          Install prod dependencies"
	@echo "  make install-dev      Install dev + test dependencies"
	@echo "  make test             Run all unit tests"
	@echo "  make test-cov         Tests with coverage report"
	@echo "  make lint             Lint with flake8"
	@echo "  make format           Format with black"
	@echo "  make validate         Validate SAM template"
	@echo "  make build            SAM build"
	@echo "  make deploy           SAM build + deploy"
	@echo "  make destroy          Delete CloudFormation stack"
	@echo "  make dry-run          Run locally (NO AWS changes)"
	@echo "  make scan-stack       Run locally (LIVE changes)"
	@echo "  make setup-ssm        Create SSM parameters"
	@echo "  make logs             View recent Lambda logs"
	@echo "  make tail-logs        Live tail Lambda logs"
	@echo "  make invoke           Invoke Lambda directly (dry-run)"
	@echo "  make clean            Remove build artifacts"
	@echo ""

install:
	pip install -r requirements.txt

install-dev:
	pip install -r requirements.txt -r requirements-dev.txt

test:
	PYTHONPATH=src pytest tests/ -v --tb=short

test-cov:
	PYTHONPATH=src pytest tests/ -v --tb=short \
		--cov=src --cov-report=term-missing \
		--cov-report=html:htmlcov --cov-fail-under=70
	@echo "Coverage report: htmlcov/index.html"

lint:
	flake8 src/ tests/ --max-line-length=120 --ignore=E501,W503

format:
	black src/ tests/ scripts/ --line-length=120

validate:
	sam validate --template infra/template.yaml --region $(REGION)

build:
	sam build --template infra/template.yaml

deploy: build
	sam deploy --config-file samconfig.toml --region $(REGION) \
		--parameter-overrides Environment=$(ENVIRONMENT) \
		--no-fail-on-empty-changeset

destroy:
	@echo "WARNING: Deleting stack $(STACK_NAME)"
	@read -p "Type yes to confirm: " ans && [ "$$ans" = "yes" ] || exit 1
	aws cloudformation delete-stack --stack-name $(STACK_NAME) --region $(REGION)
	aws cloudformation wait stack-delete-complete --stack-name $(STACK_NAME) --region $(REGION)

dry-run:
	PYTHONPATH=src python scripts/run_local.py --stack $(TARGET_STACK) --region $(REGION) --dry-run

scan-stack:
	@read -p "LIVE run on $(TARGET_STACK). Type yes: " ans && [ "$$ans" = "yes" ] || exit 1
	PYTHONPATH=src python scripts/run_local.py --stack $(TARGET_STACK) --region $(REGION)

setup-ssm:
	PYTHONPATH=src python scripts/setup_ssm.py --region $(REGION)

logs:
	aws logs tail /aws/lambda/cfn-drift-scanner-$(ENVIRONMENT) \
		--region $(REGION) --since 1h --format short

tail-logs:
	aws logs tail /aws/lambda/cfn-drift-scanner-$(ENVIRONMENT) \
		--region $(REGION) --follow --format short

invoke:
	aws lambda invoke \
		--function-name cfn-drift-scanner-$(ENVIRONMENT) \
		--region $(REGION) \
		--cli-binary-format raw-in-base64-out \
		--payload "{\"stack_name\":\"$(TARGET_STACK)\",\"dry_run\":true}" \
		/tmp/drift-out.json && cat /tmp/drift-out.json | python3 -m json.tool

clean:
	rm -rf .aws-sam/ htmlcov/ .coverage
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
