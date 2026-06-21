#!/usr/bin/env bash
# Deploys a minimal litellm 1.83.7 proxy whose pre-call hook overrides max_tokens
# on every chat request to (requested - DELTA), then proves the override live by
# sending requests and grepping the pod logs for the before -> after mapping.
#
# DELTA defaults to 5 (i.e. max_tokens := requested - 5). Override via env.
# Reuses the existing `litellm-secrets` secret + `litellm-postgres` service.
# Set KEEP=1 to leave the demo deployment running after the run.
set -euo pipefail

NS="${NS:-default}"
DELTA="${DELTA:-5}"
IMAGE="${IMAGE:-ghcr.io/berriai/litellm-database:v1.83.7-stable}"
NAME="litellm-mt5"
CM="litellm-mt5-config"
LOCAL_PORT="${LOCAL_PORT:-4001}"
KEEP="${KEEP:-0}"

workdir="$(mktemp -d)"
PF_PID=""
cleanup() {
  [ -n "$PF_PID" ] && kill "$PF_PID" 2>/dev/null || true
  rm -rf "$workdir"
  if [ "$KEEP" != "1" ]; then
    kubectl delete deploy "$NAME" svc "$NAME" configmap "$CM" -n "$NS" --ignore-not-found >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# 1. the minimal override hook: max_tokens := max(1, requested - DELTA)
cat >"$workdir/max_tokens_minus5.py" <<'PY'
import os

from litellm._logging import verbose_proxy_logger
from litellm.integrations.custom_logger import CustomLogger

CHAT_CALL_TYPES = frozenset({"completion", "acompletion", "anthropic_messages"})
DELTA = int(os.getenv("LITELLM_MAX_TOKENS_DELTA", "5"))


class MaxTokensMinusDelta(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if call_type not in CHAT_CALL_TYPES:
            return None
        current = data.get("max_tokens")
        if isinstance(current, int):
            new = max(1, current - DELTA)
            data["max_tokens"] = new
            verbose_proxy_logger.info(
                "max_tokens_minus5: %s -> %s (delta=-%s, call_type=%s, model=%s)",
                current, new, DELTA, call_type, data.get("model"),
            )
        return data


injector_instance = MaxTokensMinusDelta()
PY

cat >"$workdir/config.yaml" <<'YAML'
model_list:
  - model_name: mock-gpt
    litellm_params:
      model: openai/mock-gpt
      api_key: sk-dummy
      mock_response: "Hi"
litellm_settings:
  callbacks: ["max_tokens_minus5.injector_instance"]
general_settings:
  master_key: os.environ/PROXY_MASTER_KEY
YAML

echo ">> creating ConfigMap $CM"
kubectl create configmap "$CM" -n "$NS" \
  --from-file=config.yaml="$workdir/config.yaml" \
  --from-file=max_tokens_minus5.py="$workdir/max_tokens_minus5.py" \
  --dry-run=client -o yaml | kubectl apply -f -

echo ">> applying Deployment + Service ($IMAGE)"
kubectl apply -n "$NS" -f - <<YAML
apiVersion: apps/v1
kind: Deployment
metadata:
  name: $NAME
  labels: {app: $NAME}
spec:
  replicas: 1
  selector: {matchLabels: {app: $NAME}}
  template:
    metadata: {labels: {app: $NAME}}
    spec:
      initContainers:
        - name: wait-for-db
          image: busybox:1.36
          command: ["sh","-c","until nc -z litellm-postgres 5432; do echo waiting for postgres; sleep 2; done"]
      containers:
        - name: litellm
          image: $IMAGE
          imagePullPolicy: IfNotPresent
          args: ["--config","/etc/litellm/config.yaml"]
          ports: [{name: http, containerPort: 4000}]
          env:
            - {name: LITELLM_LOG, value: "INFO"}
            - {name: LITELLM_MAX_TOKENS_DELTA, value: "$DELTA"}
          envFrom:
            - secretRef: {name: litellm-secrets}
          readinessProbe:
            httpGet: {path: /health/readiness, port: http}
            initialDelaySeconds: 10
            periodSeconds: 5
            failureThreshold: 60
          volumeMounts:
            - {name: cfg, mountPath: /etc/litellm}
      volumes:
        - name: cfg
          configMap:
            name: $CM
            items:
              - {key: config.yaml, path: config.yaml}
              - {key: max_tokens_minus5.py, path: max_tokens_minus5.py}
---
apiVersion: v1
kind: Service
metadata: {name: $NAME, labels: {app: $NAME}}
spec:
  selector: {app: $NAME}
  ports: [{name: http, port: 4000, targetPort: http}]
YAML

echo ">> waiting for rollout"
kubectl rollout restart deploy/"$NAME" -n "$NS" >/dev/null
kubectl rollout status deploy/"$NAME" -n "$NS" --timeout=180s

echo ">> port-forward localhost:$LOCAL_PORT"
kubectl port-forward -n "$NS" svc/"$NAME" "$LOCAL_PORT":4000 >/dev/null 2>&1 &
PF_PID=$!
until curl -s "http://localhost:$LOCAL_PORT/health/readiness" >/dev/null 2>&1; do sleep 1; done

ver="$(kubectl exec -n "$NS" deploy/"$NAME" -- python -c "import importlib.metadata as m; print(m.version('litellm'))" 2>/dev/null | tail -1)"
echo ">> proxy litellm version: $ver"

req() {
  local route="$1" body="$2"
  curl -s "http://localhost:$LOCAL_PORT$route" \
    -H "Authorization: Bearer sk-1234" -H "Content-Type: application/json" \
    -d "$body" >/dev/null
}

echo ">> sending requests"
req /v1/chat/completions '{"model":"mock-gpt","max_tokens":100,"messages":[{"role":"user","content":"hi"}]}'
req /v1/chat/completions '{"model":"mock-gpt","max_tokens":4096,"messages":[{"role":"user","content":"hi"}]}'
req /v1/messages '{"model":"mock-gpt","max_tokens":50,"messages":[{"role":"user","content":"hi"}]}'
sleep 2

echo ">> override log lines:"
logs="$(kubectl logs -n "$NS" deploy/"$NAME" --tail=400 2>&1 | sed -r 's/\x1b\[[0-9;]*m//g' | grep max_tokens_minus5 || true)"
echo "$logs"

echo ">> assertions (delta=$DELTA):"
fail=0
for pair in "100 $((100-DELTA))" "4096 $((4096-DELTA))" "50 $((50-DELTA))"; do
  set -- $pair
  if echo "$logs" | grep -q "max_tokens_minus5: $1 -> $2 "; then
    echo "  PASS  $1 -> $2"
  else
    echo "  FAIL  $1 -> $2 (not found in logs)"
    fail=1
  fi
done

if [ "$fail" = "0" ]; then
  echo ">> RESULT: PASS on $ver"
else
  echo ">> RESULT: FAIL on $ver"
fi
exit $fail
