.PHONY: api-zip web-zip terraform-fmt terraform-validate
web-zip:
	uv run python scripts/build_web.py

api-zip:
	uv run python scripts/build_api.py

terraform-fmt:
	terraform fmt -check -recursive infra

terraform-validate:
	terraform -chdir=infra/bootstrap init -backend=false -lockfile=readonly
	terraform -chdir=infra/bootstrap validate
	terraform -chdir=infra/terraform init -backend=false -lockfile=readonly
	terraform -chdir=infra/terraform validate
