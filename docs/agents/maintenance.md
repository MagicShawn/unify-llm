# Maintenance workflow

How bugs and gaps enter the development loop for Unify LLM.

This file is the process source of truth for engineering skills (`/triage`, `/to-spec`, `/to-tickets`, `/implement`). Humans follow the same path.

## Sources of work

| Source | How it enters |
|--------|----------------|
| Usage bug | Conversation with an agent, or GitHub **Bug report** template |
| Gap / enhancement | Conversation, or GitHub **Enhancement** template |
| Security | Private contact only — never a public issue |

## Minimum bug fields

Every bug issue must contain:

1. What happened
2. Reproduction steps
3. Expected vs actual
4. Environment (OS, Python, commit, LAN/localhost, auth mode)
5. Logs / status (optional but preferred)

Missing fields → label `needs-info` until filled.

## Labels

### Product labels (what it is)

- `bug` — incorrect behavior
- `enhancement` — missing or improved capability
- `docs` — documentation / examples
- `ops` — deploy, performance, monitoring

### Triage roles (where it stands)

See [triage-labels.md](./triage-labels.md).

Product labels and triage roles are orthogonal: a bug can be `bug` + `needs-triage`.

## Lifecycle

```text
intake → needs-triage
       → needs-info        (waiting on reporter)
       → ready-for-agent   (specified enough for an agent to implement)
       → ready-for-human   (needs a human decision or manual work)
       → wontfix
```

1. **Intake**: file the issue (template or agent-created).
2. **Triage** (`/triage` or human): confirm it is real, set product label, set triage role.
3. **Specify**: for `ready-for-agent`, the issue body (or linked spec) must state the boundary and the test plan. Use `/to-spec` if the discussion already happened in chat.
4. **Split**: large items become tracer-bullet tickets with `/to-tickets`.
5. **Implement**: `/implement` drives TDD at agreed seams and runs `/code-review` before commit.
6. **Close**: fix + regression evidence in the issue, then close.

## Domain language

Before writing issue titles, PR descriptions, or test names, read [`CONTEXT.md`](../../CONTEXT.md). Use its terms; do not invent synonyms.

## What not to file as issues

- Implemented features as historical archive — read README / DESIGN / CONTEXT instead.
- Secrets, live API keys, or unredacted `config.yaml` contents.
- Private security incidents.
