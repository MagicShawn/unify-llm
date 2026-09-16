# Seed issues — protocol-first backlog

Create these on GitHub after `gh` is installed and authenticated:

```bash
gh auth login
bash scripts/create-seed-issues.sh
```

Or paste each body into the GitHub UI with the given labels.

Triage note: all start as `needs-triage`. Do not implement until moved to `ready-for-agent`.
Use `CONTEXT.md` vocabulary in follow-up discussion.

---

## 1. Cross-protocol streaming is best-effort text-delta only

**Labels:** `enhancement`, `needs-triage`
**Area:** Protocol / conversion

### Problem

When an OpenAI client calls a Model whose Provider is `type: anthropic` (or the reverse) with `stream: true`, conversion is limited to best-effort text deltas (`DESIGN.md`, `convert.py`). Events such as tool_use / tool_result / structured content do not survive the hop.

### Proposed direction

Define a complete-enough SSE event mapping for cross-protocol streaming: text deltas, message start/stop, tool call fragments, and usage. Same-protocol passthrough stays raw.

### Out of scope for the first ticket

Pixel-perfect Anthropic event taxonomy on the OpenAI surface (or vice versa). Tracer bullet: text + tool_use fragments + usage.

### Test plan

- Unit: SSE chunk sequences for both conversion directions
- Integration: mock upstream streaming both protocols; client sees tool_call args assembled

---

## 2. Cross-protocol tool-calling is not a full rewrite

**Labels:** `enhancement`, `needs-triage`
**Area:** Protocol / conversion

### Problem

DESIGN lists full tool-calling rewrite across protocol gaps as a v1 non-goal. Same-protocol tool calls pass through; cross-protocol is best-effort. Clients that mix OpenAI tools with an Anthropic Provider (or reverse) hit incomplete tool semantics.

### Proposed direction

Implement a deliberate conversion layer for tools: parameters schema, tool_use / tool_calls, tool_result / role=tool messages, and stop reasons. Document supported subsets and known gaps.

### Depends on

Issue 1 for streaming tool fragments (non-stream can ship first).

### Test plan

- Fixtures: OpenAI tools request → Anthropic Provider; reverse
- Assert stop_reason / finish_reason mapping and tool_result round-trip

---

## 3. OpenAI → Anthropic conversion drops image content

**Labels:** `bug`, `needs-triage`
**Area:** Protocol / conversion

### Problem

`convert.py` documents dropping images on the OpenAI→Anthropic text path. Multimodal requests silently lose vision input.

### Proposed direction

Map OpenAI image_url content parts to Anthropic image blocks (base64 / URL per Anthropic rules). If a part cannot be converted, fail the request with a clear error instead of silent drop.

### Test plan

- Unit: image part conversion; unsupported part → explicit error
- No silent success with empty text when images were present

---

## 4. count_tokens is a local estimate, not upstream-accurate

**Labels:** `enhancement`, `needs-triage`
**Area:** Protocol / conversion

### Problem

`/v1/messages/count_tokens` (~4 chars/token) diverges from vendor tokenizers, especially for non-Latin text and images. Documented limitation, but clients use it for context packing.

### Proposed direction

Phase 1: keep estimate, expose confidence / method in the response or docs. Phase 2 (optional): per-Provider upstream count when the vendor API offers it; cache short-lived.

### Test plan

- Existing estimate tests remain green
- If upstream path added: mock provider returns vendor count; gateway prefers it

---

## 5. max_context_tokens is informational only

**Labels:** `enhancement`, `needs-triage`, `needs-info`
**Area:** Routing / providers

### Problem

`model_limits.max_context_tokens` is documented as informational / future checks (`config.py`). The Gateway never rejects or warns on oversized prompts.

### Proposed direction

Need a product decision before implementation:

- A. Hard reject with a clear error when estimated input exceeds limit
- B. Warn only (log / header)
- C. Leave informational

Default proposal: **B**, with an opt-in config flag for **A**.

### Test plan

Blocked on decision.

---

## 6. Add a LICENSE file

**Labels:** `docs`, `needs-triage`
**Area:** Docs

### Problem

README says to add a license before publishing; repo has no LICENSE file.

### Proposed direction

Maintainer picks a license (MIT is common for this style of project) and commits `LICENSE`. Update README badge/link if any.

### Test plan

File exists at repo root; README does not contradict it.
