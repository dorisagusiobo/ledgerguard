@AGENTS.md

## Claude Code

- Use plan mode before implementation changes expected to touch more than three files.
- Planned skills in `.claude/skills/`: `safe-change`, `add-detection-rule`, and
  `db-migration`. These do not exist yet; do not claim to have run them.
- Planned hook: run offline tests after every Python edit and return failures to
  Claude for correction. Until configured, run relevant tests manually.
- Planned hook: block real financial data from entering the project. Until
  configured, enforce the synthetic-data requirement during every change.
- Skills and hooks are development aids. Runtime governance, approval gates, and
  budget enforcement must remain in application code.
