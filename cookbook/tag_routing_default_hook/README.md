# Tag-routing default-tag hook

A small proxy hook that lets **tagged requests reach untagged models**, without
adding `tags: ["default"]` to every shared deployment by hand.

## Problem

With `router_settings.enable_tag_filtering: True`, any request that carries a
tag (including a tag stored on a virtual key / team) triggers tag filtering.
If the target model has no deployment whose tag matches — e.g. a single-instance
shared model with no tags — the request is **rejected** with:

```
401  Not allowed to access model due to tags configuration. Passed model=... and tags=[...]
```

So a "tagged developer" who is meant to be routed to a specific instance of one
model gets blocked from every *other* model that isn't tag-aware.

## What the hook does

`custom_callbacks.py` runs in `async_pre_call_hook` and assigns `tags:
["default"]` to any deployment that carries **no tags**. That reuses LiteLLM's
native default-deployment fallback in `get_deployments_for_tag`, so:

- **Untagged / shared models** become reachable by any tagged request.
- **Tagged models keep strict 1:1 routing** — they already have tags, so the
  hook never touches them; a non-matching tag is still rejected.
- **Request tags are never modified** — tag-based spend-logging, budgets and
  analytics keep working (the request's own tag is still recorded).

It runs on every request (idempotent write), so models added at runtime via
`/model/new` are normalized on their next request too — LiteLLM exposes no
"model added" callback, and this avoids monkeypatching router internals.

## Usage

Mount both files next to each other and wire the callback in config
(see `example_config.yaml` in this folder):

```yaml
litellm_settings:
  callbacks: custom_callbacks.proxy_handler_instance

router_settings:
  enable_tag_filtering: True
```

```bash
docker run -d --name litellm-proxy --network <net> \
  -v "$(pwd)/example_config.yaml:/app/config.yaml:ro" \
  -v "$(pwd)/custom_callbacks.py:/app/custom_callbacks.py:ro" \
  -e DATABASE_URL="postgresql://..." \
  -p 4000:4000 \
  ghcr.io/berriai/litellm-database:main-v1.83.7-stable \
  --config /app/config.yaml --port 4000
```

## Verified behavior (litellm 1.83.7)

| Request | Result |
| --- | --- |
| tagged key → `solo-model` (single instance, no tags) | 200 → `deployment-solo` |
| tagged key → `runtime-model` added via `/model/new` without tags | 200 → `deployment-runtime` |
| `my-model` with `["instance-a"]` | 1:1 → `deployment-a` |
| `my-model` with `["instance-b"]` | 1:1 → `deployment-b` |
| `my-model` with unknown tag `["instance-c"]` | 401 rejected (strict routing preserved) |
| spend logs for `solo-model` | request tag preserved, e.g. `['instance-a', 'default', ...]` |
