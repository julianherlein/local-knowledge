---
type: log
tags: [meta]
---
# Operations log

Append-only. One line per operation, newest at the bottom. Compile sessions append here (see
`CLAUDE.md`, section 10). Never edit or delete past lines.

Format: `- YYYY-MM-DD HH:MM | <op> | <scope> | <details>`

Ops: `compile`, `cross-domain`, `lint`, `lint-fix`, `query`, `synthesis`, `hub-split`,
`fix-summary`, `manual`. The engine's own commits (`auto: ...`) are in `git log`, not here.

- {{NOW}} | manual | all | kb init created the vault skeleton
