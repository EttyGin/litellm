# Wiring the system-prompt / max_tokens hook into the Helm chart

`system_prompt_injector.py` is a LiteLLM `CustomLogger` that, on every request:

- prepends one fixed system instruction (as a `system` message on
  `/v1/chat/completions`, and as the top-level `system` field on the Anthropic
  `/v1/messages` endpoint)
- guarantees a non-empty assistant turn (fills a fallback when the model returns
  an empty message or stream)
- enforces a per-model `max_tokens` ceiling plus default

It is the same file as `deploy/minikube-system-prompt/system_prompt_injector.py`;
keep the two in sync if you edit one.

LiteLLM resolves a dotted `callbacks` entry as a file path relative to the
`config.yaml` directory (`/etc/litellm`), not as an importable module. So the
hook file must land next to `config.yaml` at `/etc/litellm/system_prompt_injector.py`.
The stock chart only mounts `config.yaml` there, so the hook is mounted as a
second file via the chart's generic `volumes` / `volumeMounts` knobs. None of
this requires editing the chart templates.

## 1. Ship the hook as its own ConfigMap

Run from this directory (`deploy/charts/litellm-helm/files`):

```bash
kubectl create configmap litellm-hooks -n <namespace> \
  --from-file=system_prompt_injector.py=system_prompt_injector.py \
  --dry-run=client -o yaml | kubectl apply -f -
```

## 2. Add to your Helm values

```yaml
# Register the callback (rendered into config.yaml)
proxy_config:
  litellm_settings:
    callbacks: ["system_prompt_injector.injector_instance"]
  # ... your existing model_list / general_settings ...

# Mount the hook next to config.yaml at /etc/litellm/system_prompt_injector.py
volumes:
  - name: litellm-hooks
    configMap:
      name: litellm-hooks
volumeMounts:
  - name: litellm-hooks
    mountPath: /etc/litellm/system_prompt_injector.py
    subPath: system_prompt_injector.py

# Configure behaviour (all optional; the hook has baked-in defaults)
envVars:
  LITELLM_LOG: "INFO"                       # so the INFO inject/cap lines show
  LITELLM_SYSTEM_PROMPT: "Your fixed instruction here."
  LITELLM_MAX_TOKENS_CAPS: '{"gpt-4o": 4096, "gpt-3.5-turbo": 1024}'
  LITELLM_MAX_TOKENS_DEFAULT_CAP: "2048"
```

`subPath` is required: it mounts the single file without hiding the rest of
`/etc/litellm`. The `max_tokens` cap is a ceiling plus default: a request above
the cap is clamped, a request that omits `max_tokens` gets the cap, and a
request below the cap is left untouched. `LITELLM_MAX_TOKENS_CAPS` is a JSON map
of `model_name -> cap`; `LITELLM_MAX_TOKENS_DEFAULT_CAP` applies to any model not
in that map. Omit both to disable the cap.

## 3. Roll it out

```bash
helm upgrade --install <release> deploy/charts/litellm-helm -n <namespace> -f your-values.yaml
kubectl rollout status deploy/<release>-litellm -n <namespace>
```

## 4. Verify

```bash
kubectl logs -n <namespace> deploy/<release>-litellm \
  | sed -r 's/\x1b\[[0-9;]*m//g' | grep system_prompt_injector
# -> system_prompt_injector: prepended fixed system prompt (call_type=acompletion, ...)
# -> system_prompt_injector: set fixed system prompt (call_type=anthropic_messages, ...)
# -> system_prompt_injector: enforced max_tokens cap (model=..., requested=..., ...)
```

If you see no lines, check in order: `LITELLM_LOG` is `INFO`; the callback is
present in the rendered `config.yaml`; the hook file is actually at
`/etc/litellm/system_prompt_injector.py` inside the pod
(`kubectl exec deploy/<release>-litellm -- ls -l /etc/litellm`); and the proxy
startup logs show no error resolving `system_prompt_injector.injector_instance`.
