# max_tokens clamp hooks

Two LiteLLM proxy pre-call hooks that stop context-window 400s of the form
`max_tokens=N cannot be greater than max_model_len`. By default they apply to
**every** chat request; a client opts OUT (asks us to honour its `max_tokens`
verbatim) by sending the header `x-honor-max-tokens: true`.

## Files

| File | What it does |
|------|--------------|
| `clamp_drop_always.py` | **Generic, zero-latency.** Removes `max_tokens` so the backend fills up to `max_model_len - prompt_tokens` itself. No token counting, no extra call, no per-model config. `max_tokens` is **not** honoured as a cap. |
| `clamp_generic_exact.py` | **Generic, exact.** Calls vLLM's `/tokenize` (URL derived from the model's `api_base`) to get the real prompt size + `max_model_len`, then clamps `max_tokens` only when it would overflow. Honours `max_tokens` when it fits. Cost: one `/tokenize` call per request with `max_tokens` (~4 ms in-cluster). |

Both: an over-long **prompt** still errors at the backend (the one case meant to fail).

## How it's wired into the chart

1. The `.py` files live in `files/`. The chart bundles every `files/*.py` into a
   ConfigMap via `sndFiles` (see `templates/configmap-snd-files.yaml`).
2. `values-clamp.yaml` mounts each hook at `/etc/litellm/<name>.py` with
   `subPath` — **required**, because the proxy adds `/etc/litellm` to `sys.path`,
   which is how the callback module gets imported. (Mounting only on `/patch`
   is **not** enough and fails with `ImportError`.)
3. `litellm_settings.callbacks` registers the active hook.

## Deploy

```bash
# from the repo root
helm upgrade --install litellm-clamp deploy/charts/litellm-helm \
  -f deploy/charts/litellm-helm/values-clamp.yaml
kubectl rollout status deploy/litellm-clamp
```

## Switch which hook is active

Edit `values-clamp.yaml` → `proxy_config.litellm_settings.callbacks`:

```yaml
callbacks: ["clamp_drop_always.proxy_handler_instance"]     # default: drop
# callbacks: ["clamp_generic_exact.proxy_handler_instance"] # exact clamp
```

Re-run the `helm upgrade` command above.

## Configuration (env, optional)

| Env | Default | Meaning |
|-----|---------|---------|
| `CLAMP_BYPASS_HEADER` | `x-honor-max-tokens` | Header a client sends (truthy) to opt OUT and keep its `max_tokens`. Set `""` to disable opt-out. |

(`clamp_generic_exact.py` also accepts `VLLM_TOKENIZE_URL` as a last-resort
tokenize endpoint if a model's `api_base` can't be resolved.)

## Test

```bash
kubectl port-forward svc/litellm-clamp 4000:4000 &

# 1) WITHOUT the header -> max_tokens dropped, backend fills the window (no 400)
curl -s http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-1234" -H "Content-Type: application/json" \
  -d '{"model":"qwen-0.5b","messages":[{"role":"user","content":"List fifty random english words separated by commas."}],"max_tokens":10}'

# 2) WITH the header -> max_tokens honoured exactly (capped at 10)
curl -s http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer sk-1234" -H "Content-Type: application/json" \
  -H "x-honor-max-tokens: true" \
  -d '{"model":"qwen-0.5b","messages":[{"role":"user","content":"List fifty random english words separated by commas."}],"max_tokens":10}'
```

Expected: request (1) returns `completion_tokens` ~= the remaining window
(e.g. 62) with `finish_reason: length`; request (2) returns exactly
`completion_tokens: 10`. Watch the decisions in the proxy log:

```bash
kubectl logs -f deploy/litellm-clamp | grep clamp_drop_always
# -> "... -> DROPPED (backend fills remaining window)"
# -> "... -> client opted out (honour max_tokens), unchanged"
```
