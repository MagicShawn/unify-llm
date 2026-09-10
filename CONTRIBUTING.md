# Contributing to Unify LLM

Thanks for your interest in improving Unify LLM.

## Before you start

1. Read [README.md](./README.md) and [DESIGN.md](./DESIGN.md).
2. Search existing issues and pull requests for duplicates.
3. For larger changes, open an issue first and describe the problem and proposed approach.

## Development setup

```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1   # Windows
# source .venv/bin/activate    # macOS / Linux
python -m pip install -r requirements.txt
cp config.example.yaml config.yaml
python scripts/smoke.py
python main.py
```

Do not commit real API keys or a filled-in `config.yaml`.

## Code guidelines

Follow the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html) in spirit:

- Prefer clear names over clever code.
- Keep functions focused; avoid deep nesting.
- Use type hints on public functions.
- Do not add comments that restate the code. Comment only non-obvious constraints.
- Match the existing layout under `unify_llm/`.
- Keep cross-protocol conversion text-focused unless you extend `convert.py` deliberately.

### Tests

Add or extend checks in `scripts/smoke.py` when you change:

- Routing or aliases
- OpenAI ↔ Anthropic conversion
- Monitor bookkeeping or token extraction
- HTTP surface behavior

Run:

```bash
python scripts/smoke.py
```

### Docs

Update the matching document when behavior changes:

| Change | Update |
|--------|--------|
| New config field | `config.example.yaml`, README, DEPLOYMENT if needed |
| New route | README API table, DESIGN if architecture changes |
| Ops or install steps | DEPLOYMENT.md |
| Dashboard or metrics | README monitoring section |

Write docs in the style described in [docs/style-guide.md](./docs/style-guide.md).

## Commit messages

Use Conventional Commits, aligned with common Google-adjacent practice:

```
type(scope): short summary

Optional body explaining why, not only what.
```

Types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `perf`, `build`, `ci`.

Examples:

```
fix(proxy): recover token usage from OpenAI streams
docs(readme): document Anthropic client base URL
feat(dashboard): add rolling p50 latency chart
```

## Pull requests

1. Create a feature branch from `main`:

   ```bash
   git checkout -b feat/short-description
   ```

2. Keep the PR focused on one change set.
3. Fill in the PR template if present:
   - Summary
   - Motivation
   - Test plan (`python scripts/smoke.py`, manual curl, etc.)
   - Screenshots for UI changes
4. Ensure the working tree has no secrets (`config.yaml` stays untracked).
5. Link related issues.

## Reporting bugs

Include:

- OS and Python version
- Proxy version or commit
- Config shape with secrets redacted
- Request path and model id
- Expected and actual behavior
- Relevant `/api/status` snapshot or logs

## Security issues

Do not file public issues for key leakage or remote exposure bugs. Contact the maintainer privately.
