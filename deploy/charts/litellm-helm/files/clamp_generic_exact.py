"""Version 2 — generic & EXACT clamp. No model_info, no magic number.

Both the context window AND the exact prompt size come from the backend itself,
so there is nothing to configure or calibrate per model:

  * tokenize endpoint is derived from each model's own `api_base`
    (`<api_base without /v1>/tokenize`) via the router — generic, not hardcoded.
  * vLLM `/tokenize` returns BOTH `count` (exact prompt tokens, chat template +
    default system prompt included) AND `max_model_len` (the real window). We
    never read `model_info` and never use a margin.

    available = max_model_len - prompt_tokens
    if requested > available:   # would 400 -> clamp to the largest that fits
        max_tokens = available
    # else honour the caller's max_tokens exactly

An over-long prompt (available <= 0) is left to error at the backend — the one
case meant to fail.

Cost: one /tokenize call per chat request that carries max_tokens (~4 ms
in-cluster; the prompt is sent to be tokenized). This is the price of being both
generic AND exact. If that latency matters more than honouring max_tokens, use
clamp_drop_always instead.

Works for any vLLM-backed model; for backends without /tokenize the request is
passed through unchanged (never broken).

Opt-OUT (default): the clamp applies to EVERY chat request, UNLESS the client
explicitly asks us to honour their max_tokens by sending the header
  x-honor-max-tokens: true
Configurable via CLAMP_BYPASS_HEADER (default "x-honor-max-tokens"); set "" to
disable opt-out entirely.

Wire up via litellm config:

    litellm_settings:
      callbacks: ["clamp_generic_exact.proxy_handler_instance"]
"""

import logging
import os
from typing import Any, Optional, Tuple

import httpx

from litellm._logging import verbose_proxy_logger
from litellm.integrations.custom_logger import CustomLogger

_LOG_PREFIX = "[clamp_generic_exact]"
_MAX_TOKEN_FIELDS = ("max_tokens", "max_completion_tokens")
FALLBACK_TOKENIZE_URL = os.getenv("VLLM_TOKENIZE_URL")  # optional last resort
_PROVIDER_PREFIXES = ("openai/", "hosted_vllm/", "vllm/", "litellm_proxy/")
# A client uses this header to explicitly opt OUT (= "honour my max_tokens").
BYPASS_HEADER = os.getenv("CLAMP_BYPASS_HEADER", "x-honor-max-tokens").lower()
_TRUTHY = {"1", "true", "yes", "on"}


def _ensure_info_level() -> None:
    if verbose_proxy_logger.getEffectiveLevel() > logging.INFO:
        verbose_proxy_logger.setLevel(logging.INFO)


def _request_headers(data: dict) -> dict:
    for src in (
        data.get("metadata"),
        data.get("litellm_metadata"),
        data.get("proxy_server_request"),
    ):
        headers = (src or {}).get("headers")
        if headers:
            return headers
    return data.get("headers") or {}


def client_opted_out(data: dict) -> bool:
    """True if the client sent the opt-out header to HONOUR their max_tokens."""
    if not BYPASS_HEADER:
        return False
    val = _request_headers(data).get(BYPASS_HEADER)  # header names arrive lower-cased
    return val is not None and str(val).lower() in _TRUTHY


def _strip_provider(model: str) -> str:
    for p in _PROVIDER_PREFIXES:
        if model.startswith(p):
            return model[len(p) :]
    return model


class ClampGenericExact(CustomLogger):
    def __init__(self, window_info: Optional[Any] = None) -> None:
        super().__init__()
        self._client = httpx.AsyncClient(timeout=10.0)
        self._window_info = window_info or self._default_window_info

    @staticmethod
    def _resolve(model: Optional[str]) -> Optional[Tuple[str, str]]:
        """(tokenize_url, served_model_name) from the model's api_base."""
        try:
            from litellm.proxy.proxy_server import llm_router

            if llm_router is None or model is None:
                return None
            for dep in llm_router.get_model_list(model_name=model) or []:
                lp = dep.get("litellm_params", {}) or {}
                api_base = lp.get("api_base")
                if not api_base:
                    continue
                base = api_base.rstrip("/")
                if base.endswith("/v1"):
                    base = base[: -len("/v1")].rstrip("/")
                served = _strip_provider(lp.get("model") or model)
                return base + "/tokenize", served
        except Exception:
            return None
        return None

    async def _tokenize(
        self, data: dict, url: str, served: str
    ) -> Optional[Tuple[int, int]]:
        payload: dict[str, Any] = {"model": served}
        if data.get("messages") is not None:
            payload["messages"] = data["messages"]
        elif data.get("prompt") is not None:
            payload["prompt"] = data["prompt"]
        else:
            return None
        resp = await self._client.post(url, json=payload)
        resp.raise_for_status()
        body = resp.json()
        count, mml = body.get("count"), body.get("max_model_len")
        if count is None or mml is None:
            return None
        return int(count), int(mml)

    async def _default_window_info(
        self, data: dict, model: Optional[str]
    ) -> Optional[Tuple[int, int]]:
        backend = self._resolve(model)
        if backend is None and FALLBACK_TOKENIZE_URL:
            backend = (FALLBACK_TOKENIZE_URL, _strip_provider(model or ""))
        if backend is None:
            verbose_proxy_logger.warning(
                "%s model=%s -> no tokenize endpoint, passing through",
                _LOG_PREFIX,
                model,
            )
            return None
        url, served = backend
        try:
            return await self._tokenize(data, url, served)
        except Exception as e:
            verbose_proxy_logger.warning(
                "%s model=%s -> tokenize failed (%s) at %s, passing through",
                _LOG_PREFIX,
                model,
                type(e).__name__,
                url,
            )
            return None

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict,
        call_type: str,
    ) -> Optional[dict]:
        field = next((f for f in _MAX_TOKEN_FIELDS if data.get(f) is not None), None)
        if field is None:
            return data
        requested = data[field]
        _ensure_info_level()
        model = data.get("model")
        if client_opted_out(data):
            verbose_proxy_logger.info(
                "%s model=%s -> client opted out (honour max_tokens), unchanged",
                _LOG_PREFIX,
                model,
            )
            return data

        info = await self._window_info(data, model)
        if info is None:
            return data

        prompt_tokens, max_model_len = info
        available = max_model_len - prompt_tokens
        if available <= 0:
            verbose_proxy_logger.info(
                "%s model=%s prompt_tokens=%s max_model_len=%s -> PROMPT "
                "OVERFLOWS, unchanged (will 400)",
                _LOG_PREFIX,
                model,
                prompt_tokens,
                max_model_len,
            )
            return data
        if requested > available:
            data[field] = available
            verbose_proxy_logger.info(
                "%s model=%s prompt_tokens=%s max_model_len=%s -> CLAMPED "
                "%s %s -> %s",
                _LOG_PREFIX,
                model,
                prompt_tokens,
                max_model_len,
                field,
                requested,
                available,
            )
        else:
            verbose_proxy_logger.info(
                "%s model=%s prompt_tokens=%s max_model_len=%s available=%s "
                "-> HONOURED (fits)",
                _LOG_PREFIX,
                model,
                prompt_tokens,
                max_model_len,
                available,
            )
        return data


proxy_handler_instance = ClampGenericExact()
