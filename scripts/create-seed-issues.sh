#!/usr/bin/env bash
# Create seed backlog issues on GitHub. Requires: gh auth login
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v gh >/dev/null 2>&1; then
  echo "gh CLI not found. Install GitHub CLI first: https://cli.github.com/" >&2
  exit 1
fi

# Ensure labels exist (idempotent)
ensure_label() {
  local name="$1" color="$2" desc="$3"
  gh label create "$name" --color "$color" --description "$desc" --force >/dev/null
}

ensure_label bug "d73a4a" "Incorrect behavior"
ensure_label enhancement "a2eeef" "Missing or improved capability"
ensure_label docs "0075ca" "Documentation"
ensure_label ops "fbca04" "Deploy / performance / monitoring"
ensure_label needs-triage "ededed" "Maintainer needs to evaluate"
ensure_label needs-info "d4c5f9" "Waiting on reporter"
ensure_label ready-for-agent "0e8a16" "Specified enough for an agent"
ensure_label ready-for-human "1d76db" "Needs a human"
ensure_label wontfix "ffffff" "Will not be actioned"

create() {
  local title="$1" labels="$2" body_file="$3"
  gh issue create --title "$title" --label "$labels" --body-file "$body_file"
}

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

# 1
cat >"$tmpdir/1.md" <<'EOF'
## Problem

When an OpenAI client calls a Model whose Provider is `type: anthropic` (or the reverse) with `stream: true`, conversion is limited to best-effort text deltas. Tool_use / tool_result / structured content do not survive the hop.

## Proposed direction

Define a complete-enough SSE event mapping for cross-protocol streaming: text deltas, message start/stop, tool call fragments, and usage. Same-protocol passthrough stays raw.

## Out of scope (first tracer bullet)

Pixel-perfect Anthropic event taxonomy on the OpenAI surface (or vice versa). Ship: text + tool_use fragments + usage.

## Test plan

- Unit: SSE chunk sequences both directions
- Integration: mock upstream streaming; client sees tool_call args assembled

Related vocabulary: Gateway, Protocol, Model, Provider — see CONTEXT.md.
EOF
create "Cross-protocol streaming is best-effort text-delta only" "enhancement,needs-triage" "$tmpdir/1.md"

# 2
cat >"$tmpdir/2.md" <<'EOF'
## Problem

Full tool-calling rewrite across protocol gaps is a v1 non-goal. Same-protocol tool calls pass through; cross-protocol is best-effort.

## Proposed direction

Deliberate conversion layer for tools: parameters schema, tool_use / tool_calls, tool_result / role=tool messages, stop reasons. Document supported subsets and known gaps.

## Depends on

Streaming tool fragments can follow; non-stream conversion may ship first.

## Test plan

- Fixtures: OpenAI tools → Anthropic Provider and reverse
- stop_reason / finish_reason mapping and tool_result round-trip

Related vocabulary: Protocol, Provider, Model — see CONTEXT.md.
EOF
create "Cross-protocol tool-calling is not a full rewrite" "enhancement,needs-triage" "$tmpdir/2.md"

# 3
cat >"$tmpdir/3.md" <<'EOF'
## Problem

`convert.py` drops images on the OpenAI→Anthropic text path. Multimodal requests silently lose vision input.

## Proposed direction

Map OpenAI `image_url` content parts to Anthropic image blocks (base64 / URL per Anthropic rules). If a part cannot be converted, fail with a clear error instead of silent drop.

## Test plan

- Unit: image part conversion; unsupported part → explicit error
- No silent success with empty text when images were present

Related vocabulary: Protocol, Gateway — see CONTEXT.md.
EOF
create "OpenAI → Anthropic conversion drops image content" "bug,needs-triage" "$tmpdir/3.md"

# 4
cat >"$tmpdir/4.md" <<'EOF'
## Problem

`/v1/messages/count_tokens` (~4 chars/token) diverges from vendor tokenizers, especially non-Latin text and images. Clients use it for context packing.

## Proposed direction

Phase 1: keep estimate, expose method/limits in docs (and optionally response). Phase 2: per-Provider upstream count when the vendor API offers it.

## Test plan

- Existing estimate tests remain green
- If upstream path added: mock returns vendor count; gateway prefers it

Related vocabulary: Protocol, Upstream, Model — see CONTEXT.md.
EOF
create "count_tokens is a local estimate, not upstream-accurate" "enhancement,needs-triage" "$tmpdir/4.md"

# 5
cat >"$tmpdir/5.md" <<'EOF'
## Problem

`model_limits.max_context_tokens` is informational; the Gateway never rejects or warns on oversized prompts.

## Proposed direction (needs decision)

- A. Hard reject when estimated input exceeds limit
- B. Warn only (log / header)
- C. Leave informational

Proposal: **B** default, opt-in config flag for **A**.

## Test plan

Blocked on product decision.
EOF
create "max_context_tokens is informational only" "enhancement,needs-triage,needs-info" "$tmpdir/5.md"

# 6
cat >"$tmpdir/6.md" <<'EOF'
## Problem

README says to add a license before publishing; repo has no LICENSE file.

## Proposed direction

Maintainer picks a license (MIT is common for this style of project) and commits `LICENSE`. Align README if needed.

## Test plan

File exists at repo root; README does not contradict it.
EOF
create "Add a LICENSE file" "docs,needs-triage" "$tmpdir/6.md"

echo "Seed issues created."
EOF
