@AGENTS.md

## Claude Code

- Use plan mode before implementation changes expected to touch more than three files.
- Use `.claude/skills/safe-change/SKILL.md` for code, test and infrastructure changes.
- Planned skills: `add-detection-rule` and `db-migration`; these do not exist yet.
- Configured hooks in `.claude/settings.json`: run offline tests after Python
  Write/Edit calls and block suspected financial identifiers before Write/Edit.
- Use nonnumeric synthetic identifiers (for example TEST-CARD-001). The guard
  cannot prove data is fake and does not cover Bash writes or external editors.
- Hook scripts are locally tested; confirm activation in Claude Code with `/hooks`.
- Skills and hooks are development aids. Runtime governance, approval gates, and
  budget enforcement must remain in application code.
