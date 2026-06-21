# LiteLLM on Minikube with a fixed system-prompt injector

Deploys the LiteLLM proxy to the `default` namespace with a custom `CustomLogger`
that prepends one fixed system message to every request and guarantees a
non-empty assistant turn. It covers both `/v1/chat/completions` (where the
instruction is prepended as a `system` message) and the Anthropic `/v1/messages`
endpoint (Anthropic has no `system` role inside `messages`, so there the
instruction is merged into the top-level `system` field instead). The same hook
also enforces a per-model `max_tokens` ceiling, configured at the deployment
level.

The model list uses LiteLLM's built-in `mock_response`, so the deployment needs
no real provider key and makes no outbound LLM calls: `mock-gpt` always answers
`Hi`, and `mock-empty` returns a blank turn to exercise the empty-response guard.

Files:
- `system_prompt_injector.py` - the `CustomLogger` hook (`injector_instance`)
- `sitecustomize.py` - a transparent debug wrapper around
  `get_modified_max_tokens`. It does not change the function's logic; it only
  prints the incoming `max_tokens` on entry and the value the original function
  returned on exit. Loaded via `PYTHONPATH=/patch` (no new image), and the
  wrapper fires only when `litellm_settings.modify_params: true`
- `config.yaml` - proxy config wiring the hook via `litellm_settings.callbacks`
- `manifest.yaml` - Postgres (Deployment + Service) and the proxy (Deployment +
  Service). Postgres backs virtual keys, budgets, teams and the admin UI; the
  proxy waits for it via an initContainer and runs the Prisma schema on startup.
  Storage is an `emptyDir`, so the DB is ephemeral - fine for local Minikube

The hook ships next to `config.yaml` because LiteLLM resolves a dotted
`callbacks` entry as a file path relative to the config directory, not as an
importable module. The fixed instruction is read from the `LITELLM_SYSTEM_PROMPT`
env var (set on the Deployment) and falls back to a default baked into the hook.

The hook adjusts `max_tokens` from the deployment env, and this deployment uses
`LITELLM_MAX_TOKENS_DELTA: "10"`: every request that carries `max_tokens` has it
reduced by 10 (floored at 1), so a caller asking for 100 is sent 90. It applies
to both `/v1/chat/completions` and `/v1/messages`.

Two more knobs exist for a per-model ceiling instead of (or on top of) the delta:
`LITELLM_MAX_TOKENS_CAPS` is a JSON map of `model_name -> cap` and
`LITELLM_MAX_TOKENS_DEFAULT_CAP` applies to models not in that map; a cap clamps
requests above it and supplies a default when `max_tokens` is omitted. The delta
is applied first, then the cap. Leave a knob unset to disable it. LiteLLM's
native clamp (`modify_params` + the model registry) only shrinks toward a model's
published output limit, so it cannot reduce by a fixed amount or supply a
deployment-chosen value; the hook covers that.

## Deploy

```bash
cd deploy/minikube-system-prompt

# 1. Make the image available to the cluster
minikube image load ghcr.io/berriai/litellm-database:v1.89.0

# 2. Ship config.yaml + the hook + the max_tokens debug wrapper as one ConfigMap
kubectl create configmap litellm-config -n default \
  --from-file=config.yaml=config.yaml \
  --from-file=system_prompt_injector.py=system_prompt_injector.py \
  --from-file=sitecustomize.py=sitecustomize.py \
  --dry-run=client -o yaml | kubectl apply -f -

# 3. Master key + local Postgres credentials (dummy local values, not real secrets)
kubectl create secret generic litellm-secrets -n default \
  --from-literal=PROXY_MASTER_KEY=sk-1234 \
  --from-literal=POSTGRES_USER=litellm \
  --from-literal=POSTGRES_PASSWORD=litellm \
  --from-literal=POSTGRES_DB=litellm \
  --from-literal=DATABASE_URL=postgresql://litellm:litellm@litellm-postgres:5432/litellm \
  --dry-run=client -o yaml | kubectl apply -f -

# 4. Apply Postgres + proxy
kubectl apply -f manifest.yaml
kubectl rollout status deploy/litellm-postgres -n default
kubectl rollout status deploy/litellm-system-prompt -n default
```

## Verify

```bash
kubectl get pods -n default -l app=litellm-system-prompt
kubectl port-forward -n default svc/litellm-system-prompt 4000:4000 &

# Always answers "Hi"; the caller's message is preserved
curl -s http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-1234" -H "Content-Type: application/json" \
  -d '{"model":"mock-gpt","messages":[{"role":"user","content":"What is 2+2?"}]}' \
  | jq '.choices[0].message.content'

# Empty-response guard: blank model turn becomes a non-empty fallback
curl -s http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-1234" -H "Content-Type: application/json" \
  -d '{"model":"mock-empty","messages":[{"role":"user","content":"hi"}]}' \
  | jq '.choices[0].message.content'

# Anthropic /v1/messages: instruction goes into the top-level system field,
# the request still returns a valid Anthropic response, and max_tokens=50000
# is clamped to the per-model cap (1024)
curl -s http://localhost:4000/v1/messages \
  -H "Authorization: Bearer sk-1234" -H "Content-Type: application/json" \
  -d '{"model":"mock-gpt","max_tokens":50000,"messages":[{"role":"user","content":"hi"}]}' \
  | jq '.content'
```

The proxy logs the injection at INFO (`LITELLM_LOG=INFO` is set on the
Deployment), so the prepend is greppable without DEBUG noise:

```bash
kubectl logs -n default deploy/litellm-system-prompt -f \
  | sed -r 's/\x1b\[[0-9;]*m//g' | grep --line-buffered system_prompt_injector
# -> system_prompt_injector: prepended fixed system prompt (call_type=acompletion, messages=2) -> '...'
# -> system_prompt_injector: reduced max_tokens by 10 (model=mock-gpt, requested=50000) -> 49990
# -> system_prompt_injector: set fixed system prompt (call_type=anthropic_messages) -> '...'
```

## max_tokens debug wrapper

`config.yaml` sets `modify_params: true`, so the proxy runs its `CHECK MAX
TOKENS` path and calls `get_modified_max_tokens` on every chat request that
carries `max_tokens`. `sitecustomize.py` wraps that function transparently and
logs both ends without altering the result:

```bash
kubectl exec -n default deploy/litellm-system-prompt -- \
  python -c "import litellm._lazy_imports as l; print(l._get_modified_max_tokens_func)"
# -> <function _debug_get_modified_max_tokens ...>

kubectl port-forward -n default svc/litellm-system-prompt 4000:4000 &
curl -s http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-1234" -H "Content-Type: application/json" \
  -d '{"model":"mock-gpt","messages":[{"role":"user","content":"hi"}],"max_tokens":4096}' >/dev/null

kubectl logs -n default deploy/litellm-system-prompt --tail=200 \
  | sed -r 's/\x1b\[[0-9;]*m//g' | grep maxtokens-debug
# [litellm-maxtokens-debug] ENTER model=openai/mock-gpt max_tokens(in)=4096
# [litellm-maxtokens-debug] EXIT  model=openai/mock-gpt max_tokens(in)=4096 max_tokens(out)=4096
```

For a mock model the value comes back unchanged; point a registry-backed model
at it to watch the original heuristic shrink `max_tokens(out)`. `sitecustomize`
loads in every Python process in the pod (including the Prisma migration shell),
so the install marker also appears once in the migrate output; that is harmless.
To disable, set `modify_params: false` (or drop `PYTHONPATH=/patch`) and roll.

With Postgres connected, virtual keys work (they failed with `No connected db`
in the DB-less variant). Generate one with the master key, then use it:

```bash
VK=$(curl -s http://localhost:4000/key/generate \
  -H "Authorization: Bearer sk-1234" -H "Content-Type: application/json" \
  -d '{"models":["mock-gpt"],"max_budget":1}' | jq -r '.key')

curl -s http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer $VK" -H "Content-Type: application/json" \
  -d '{"model":"mock-gpt","messages":[{"role":"user","content":"hi"}]}' \
  | jq '.choices[0].message.content'
```

## Tear down

```bash
kubectl delete -f manifest.yaml
kubectl delete configmap litellm-config -n default
kubectl delete secret litellm-secrets -n default
```
