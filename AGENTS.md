## Agent skills

### Issue tracker

GitHub Issues (`MagicShawn/unify-llm`) via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: root `CONTEXT.md` + `docs/adr/`. Use glossary terms in issue titles, PRs, and test names. See `docs/agents/domain.md`.

### Maintenance

Bugs and gaps enter via GitHub issues (templates under `.github/ISSUE_TEMPLATE/`). Lifecycle: intake → triage roles → ready-for-agent → implement. See `docs/agents/maintenance.md`.
