# Unify LLM

Local multi-provider LLM Gateway: one fixed client-facing port, routing to many vendor upstreams under two protocol surfaces.

## Language

### Core

**Gateway**:
This service — the single local entry point clients call instead of each vendor API.
_Avoid_: proxy server (too generic), hub

**Provider**:
One configured upstream integration in this Gateway (type, base URL, credential, model list, enabled flag).
_Avoid_: vendor config, channel

**Upstream**:
The remote vendor API a Provider actually talks to.
_Avoid_: backend, origin

**Model**:
A model id that the Gateway routes to exactly one enabled Provider.
_Avoid_: engine

**Alias**:
A friendly name that resolves to a Model; resolves one hop only.
_Avoid_: nickname, shortcut model

**Protocol**:
The client-facing API surface: OpenAI Chat Completions or Anthropic Messages.
_Avoid_: dialect, wire format (for the surface itself)

### Access

**User**:
A Portal account with role `admin` or `user`.
_Avoid_: member, account holder

**API Key**:
A per-User `sk-unify-…` credential used to call `/v1` routes.
_Avoid_: user token, access token (session cookie is separate)

**Gateway Key**:
The shared LAN credential (`UNIFY_GATEWAY_KEY`) protecting the whole Gateway; distinct from any User API Key.
_Avoid_: master key, admin key

**Session**:
A Portal login cookie for dashboard/portal UI; not used for `/v1` API calls.
_Avoid_: login token

### Quota and limits

**Points**:
Per-User request quota units deducted after a successful call; insufficient Points reject with payment required.
_Avoid_: credits, balance (balance may be read as USD)

**Rate Limit**:
Admission control on client traffic: per-client requests-per-minute and global concurrency cap.
_Avoid_: throttle policy (unless naming the config block)

### Reliability

**Fallback**:
A backup Model used after the primary Model's retries are exhausted on a hard failure.
_Avoid_: backup model, failover model

**In-flight**:
A request that has been admitted and is waiting on an Upstream response; visible on the dashboard.
_Avoid_: active request, pending call

### Maintenance

**Triage role**:
The lifecycle label on a work item: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, or `wontfix`.
_Avoid_: status, workflow stage

**Gap**:
A known shortfall versus the intended product boundary (incomplete feature, limitation, or missing test); tracked as a GitHub issue, not as a glossary term.
_Avoid_: debt (too vague), bug (bugs are a kind of gap but not all gaps are bugs)
