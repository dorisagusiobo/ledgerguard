---
name: safe-change
description: Use before editing, fixing, refactoring or adding features to LedgerGuard code, tests or Terraform in infra/. Establishes a test baseline and reviews infrastructure changes.
---

# Safe change checklist for LedgerGuard

## Before making a change

Read AGENTS.md and docs/ARCHITECTURE.md. Check `git status --short` to preserve existing work, then read the code and tests related to the request.

Run `uv run pytest -q` before editing code or infrastructure. Record existing failures. Exit code 5 means no tests were collected: report “no tests,” neither a failure nor a pass.

## After making a change

Run `uv run pytest -q` again and report the result, including “no tests” if applicable.

If files under infra/ changed, run `terraform -chdir=infra plan` only when infra/ contains .tf files. Otherwise report that no Terraform configuration exists yet. Read the entire plan, especially destruction and replacement actions; replacement also destroys the existing resource.

If the change touched money, citations, the governance gateway or budgets, confirm that a meaningful test covers the changed behavior. Add one if missing.

Investigate test failures within the requested scope and rerun the tests. Never delete or weaken tests to make them pass. If resolution needs unrelated changes or approval, explain what is needed. Do not apply infrastructure changes with failing tests or a failed plan.

## Stop and ask me if

- Test failures cannot be resolved within scope or require my decision.
- The Terraform plan would destroy or replace a resource.
- A change would edit IAM roles or policies, AWS Budgets actions or alert settings.
- A change would alter the $10/month, $0.50/run or 20-call caps, including effective values changed through configuration or environment overrides.
- A change would alter governance gateway behavior or the allowed-tools list.
- A tool could write financial records, or an investigator tool would become non-read-only.
- A dependency needs to be added.
- You are ready to run `terraform apply`: present the specific plan and obtain explicit approval for it. If the plan changes, obtain approval again.

Follow AGENTS.md's requirement for action-specific approval. A general task request does not authorize an item on this list.

## Working practices

1. Identify the requested behavior and the smallest change that achieves it. Follow existing patterns and avoid unrelated refactors.
2. Keep changes within Version 1 unless I explicitly expand the scope.
3. Preserve the code rules in AGENTS.md (Decimal money, gateway on every call, citations, human decision, append-only audit log).
4. Keep credentials and real financial data out of logs, fixtures and responses. Use synthetic fixtures and fakes for offline tests.
5. Add or adjust regression tests when behavior changes. Run affected tests while iterating; report checks you could not run.
6. For infrastructure edits, review the diff and run formatting and validation checks when tools and configuration exist. Treat live Bedrock calls and cloud resources as separate actions requiring authorization; ordinary verification stays offline.
7. Review the final diff for unintended changes and exposed secrets. Report changes, checks, results and limitations. Commit or push only with specific approval.
