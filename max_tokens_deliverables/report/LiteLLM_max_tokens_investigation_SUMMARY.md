# LiteLLM `max_tokens` Truncation: Investigation Summary

**Scope:** LiteLLM 1.88.1 (audited against the live working tree and fork).
**Outcome:** Root cause isolated, fix implemented and tested in our fork, three complementary remediation paths defined.

---

## 1. The Problem and Symptom

Agent clients (both OpenAI-format agents on `/chat/completions` and Claude Code on the Anthropic `/v1/messages` path) were experiencing two distinct failures:

- **Silent truncation** — responses cut off mid-output with `finish_reason: "length"` (OpenAI format) or `stop_reason: "max_tokens"` (Anthropic format). The cut got worse the longer a session ran, because it scales with prompt size.
- **Hard failure** — some requests rejected outright with `ContextWindowExceeded` / a provider 400, often with the arithmetic landing at exactly `prompt + max_tokens = context_window + 1`.

**The proof it was LiteLLM, not the client or provider:** with `--detailed_debug`, the proxy emits a literal `MODIFYING MAX TOKENS` log line showing the original `user_max_tokens`, the counted `input_tokens`, and the model's `max_output_tokens`. Comparing the client-sent `max_tokens` against the raw payload leaving the proxy showed they differed.

---

## 2. Root Cause

The dynamic rewrite lives in `get_modified_max_tokens` in `litellm/litellm_core_utils/token_counter.py`. It runs only when `litellm.modify_params` (env `LITELLM_MODIFY_PARAMS`) is enabled, invoked from the `@client` wrapper in `litellm/utils.py` (the `CHECK MAX TOKENS` blocks), and applies to `completion`, `acompletion`, and `anthropic_messages` call types.

The faulty heuristic is **Case 1**:

```python
## CASE 1: model input + output can't exceed X
if _model_info["max_input_tokens"] == max_output_tokens:
    ...
    user_max_tokens = int(max_output_tokens - input_tokens)
```

- The equality `max_input_tokens == max_output_tokens` is used as a proxy for "this model has a single shared window" (true for legacy gpt-3.5-turbo-style completion models).
- It then subtracts the prompt from the output cap, minus a thin buffer: `max(0.1 * input_tokens, 10)`, computed from a local token estimate.

**Why it misfires:** hundreds of modern chat models list equal input/output limits in the registry while actually having independent budgets (e.g. 256K/256K entries, and our own 200,000-token configuration). For these, LiteLLM wrongly subtracts the entire prompt from the output cap. As the conversation grows, `max_tokens` shrinks and responses truncate. The buffer makes it worse: `tiktoken` undercounts non-OpenAI tokenizers by 15-20%, larger than the 10% buffer, so the safety margin is insufficient on Claude/Llama prompts.

**Accuracy note (important):** the silent truncation is a confirmed LiteLLM bug. The hard `ContextWindowExceeded` "+1" failure is **not** a LiteLLM off-by-one — that arithmetic is computed by the provider with its real tokenizer and merely relayed by LiteLLM. There is no pre-flight `prompt + max_tokens vs window` check in LiteLLM to contain an off-by-one. The "+1 over" pattern is the fingerprint of a client computing fill-the-window `max_tokens` and overshooting by one token of estimation error. LiteLLM's only real contribution to the hard case is that its undercount-prone buffer can leave the rewritten value over budget on large prompts.

---

## 3. Trigger Conditions and Blast Radius

- Fires **only** when `modify_params: true`.
- Turning `modify_params` off globally is **not** recommended: the flag is overloaded and also controls genuinely useful behaviors (Anthropic tool-call repair, message sanitization, placeholder/dummy-message insertion, thinking-param dropping). On Claude Code's native `/v1/messages` passthrough, the message-mutation behaviors do not run, but the max_tokens rewrite does.
- Whether the rewrite does damage depends on each deployment's registry data (`model_info`).

---

## 4. Solution Options

### Option A — Registry Configuration Fix (zero code)
Override `model_info` per deployment so input and output limits are realistic and distinct, breaking the equality so Case 1 can never trigger.

```yaml
model_list:
  - model_name: my-model
    litellm_params: { model: ... }
    model_info:
      max_input_tokens: 200000
      max_output_tokens: 4096   # or 64000; just not equal to input
```
**Steps:** (1) find every model where `max_input_tokens == max_output_tokens`; (2) set correct distinct values; (3) reload the proxy and confirm `MODIFYING MAX TOKENS` is gone.
**Pros:** no code change; also improves routing and cost tracking. **Cons:** depends on configuration discipline across every entry.

### Option B — Upstream Code Fix (the architectural fix)
Add one condition so the shrink only applies to genuine legacy models:

```python
if (
    _model_info["max_input_tokens"] == max_output_tokens
    and _model_info.get("mode") == "completion"
):
    ...  # only legacy shared-window models shrink
```
Verified against the live registry: completion-mode models (e.g. `command`) still shrink correctly; chat models with equal limits (e.g. `ai21.jamba-1-5-large-v1:0`) now pass through. Implemented in our fork with regression tests covering both scenarios; ruff and black clean. Path to an upstream PR so we drop the private patch.
**Caveat for reviewers:** `mode == "completion"` is a heuristic proxy, not a guarantee. If a genuinely shared-window model were ever registered as `chat`, it would skip the shrink and rely on provider enforcement — acceptable, since the provider enforces the window authoritatively anyway.

### Option C — Runtime Monkey Patch (immediate production stopgap)
At proxy startup, replace the cached function pointer with a passthrough. Neutralizes only the rewrite; every other `modify_params` behavior is untouched; survives `pip install -U litellm`.

```python
# litellm_patch.py  -  loaded once at proxy startup
import litellm._lazy_imports as _li

def _passthrough_max_tokens(*, user_max_tokens=None, **kwargs):
    return user_max_tokens

_li._get_modified_max_tokens_func = _passthrough_max_tokens
```
Load it via `litellm_settings: callbacks: ["litellm_patch"]` (the module must be on the proxy's `PYTHONPATH`). The `**kwargs` absorbs all six keyword arguments the wrapper passes. The patch point is legitimate because the function is resolved through a lazy-import cache (`litellm/_lazy_imports.py::_get_modified_max_tokens_func`).

---

## 5. Options Compared

| Option | Effort | Risk | Scope | Permanence |
|---|---|---|---|---|
| A. Registry config | Low | Low | Per-deployment | Depends on discipline |
| B. Upstream fix | Medium | Low | Global, correct | High; retires the fork |
| C. Monkey patch | Low | Very low | Global | Immediate stopgap |

They are complementary, not mutually exclusive.

---

## 6. Recommendation and Next Steps

**Layered rollout:**
1. **Immediately** — deploy Option C (runtime passthrough) to stop production truncation today.
2. **In parallel** — apply Option A; correct `model_info` for our deployments, especially the 200,000-token config.
3. **Near term** — land Option B upstream so the fix is permanent and we retire the private patch.

**Validation (per our proof-of-fix convention):** run a real proxy against a real provider with an equal input/output deployment, send a large `max_tokens`, and show the raw provider payload carrying the value unmodified. Before: logs `MODIFYING MAX TOKENS` with a shrunken value. After: forwards the client's value intact.

**Action items:**
- Apply the runtime patch to production config; confirm `MODIFYING MAX TOKENS` no longer appears.
- Audit all `model_info` entries for input/output equality and correct them.
- Open the upstream PR from the fork branch with the conditional fix and regression tests.
- Add monitoring that alerts if `finish_reason: "length"` rates rise again.
- Keep `modify_params` on (recommended) to retain Anthropic message-repair behaviors.

---

## 7. Technical Reference

| Item | Location |
|---|---|
| Faulty function | `litellm/litellm_core_utils/token_counter.py::get_modified_max_tokens` |
| Wrapper gate | `litellm/utils.py` — the `CHECK MAX TOKENS` blocks (gated on `modify_params` + call type) |
| Monkey-patch point | `litellm/_lazy_imports.py::_get_modified_max_tokens_func` |
| Diagnostic toggle | `litellm_settings: log_raw_request_response: true` |
| Version audited | 1.88.1 |

---

## 8. Fork Change Summary (what we actually implemented)

- **Source:** `get_modified_max_tokens` gained the `mode == "completion"` condition on Case 1 (Option B). All original baseline logic preserved; the function signature is unchanged because the `@client` wrapper calls it with all six keyword arguments.
- **Tests:** `tests/test_litellm/litellm_core_utils/test_token_counter.py::test_get_modified_max_tokens` extended to cover both scenarios — legacy completion model still shrinks (`command, 4000, 256 -> 96`), chat model with equal limits passes through (`ai21.jamba-1-5-large-v1:0, 250000, 256000 -> 256000`). The jamba row is the regression guard: it returns 6000 under the old unconditional Case 1, so it fails if the gate is ever removed.
- **Branch:** `litellm_max_tokens_passthrough` (renamed from `fix/max-tokens-passthrough` to follow repo naming conventions: no slash, `litellm_` prefix).
- **Quality:** full token_counter test file passes (83 passed, 1 skipped); ruff clean; black clean on changed lines.
