# Documentation style guide

This project follows a practical subset of the [Google developer documentation style guide](https://developers.google.com/style) and the [Google Python style guide](https://google.github.io/styleguide/pyguide.html).

## Voice

- Use second person (“you”).
- Use present tense.
- Prefer active voice.
- Be direct. Lead with the action or outcome.

**Prefer:** Start the gateway with `python main.py`.  
**Avoid:** The gateway can be started by running `python main.py`.

## Structure

- Put the most common task near the top of a page.
- Use short paragraphs. Prefer one idea per paragraph.
- Use tables for field references and path lists.
- Use fenced code blocks with a language tag (`python`, `bash`, `yaml`).
- Keep headings sentence case: “Quick start”, not “Quick Start” or “QUICK START”.

## Terminology

| Use | Avoid |
|-----|--------|
| API key | token, secret key (unless it is truly a secret) |
| upstream | backend vendor (in user docs) |
| model id | model name string |
| Base URL | endpoint root |
| request | call, hit |

Capitalize product names as vendors do: OpenAI, Anthropic, DeepSeek, GLM.

## Placeholders

- Show secrets as `${ENV_VAR}` or `...`.
- Never paste a real key into docs, examples, screenshots, or issues.
- Use `127.0.0.1` instead of `localhost` in URLs for clarity.

## Code samples

- Samples must be copy-pasteable.
- Prefer complete minimal examples over fragments.
- Default port is `8787`; keep examples consistent unless you state another port.

## Commits and PR text

- Subject line: imperative mood, under about 72 characters.
- Explain motivation in the body when the change is not obvious.
- Do not list every file touched unless it helps review.

## Diagrams

- Use Mermaid or self-contained SVG for architecture.
- Label boxes with user-facing names.
- Keep diagrams small enough to read inline.
