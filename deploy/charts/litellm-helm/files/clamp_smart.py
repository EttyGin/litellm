"""Version 4 — SMART clamp. Exact, but skips the /tokenize call when the request
comfortably fits the window.

Generic for ANY vLLM-backed model. The window (max_model_len) is learned once
per model from vLLM's own /tokenize response and cached (it is static). For each
request a cheap LOCAL token estimate decides:

  * estimate * SLACK + max_tokens + MARGIN <= window  -> comfortably fits; skip
    the /tokenize call and pass the request through untouched.
  * otherwise (near the window, or the window not learned yet) -> do the exact
    /tokenize and clamp to max_model_len - prompt_tokens, exactly like
    clamp_generic_exact. This is the only path that can clamp, and it is exact,
    so a request is NEVER rejected for being a token over.

So requests with plenty of headroom pay nothing; only borderline requests pay
the tokenize. It is never more expensive than clamp_generic_exact, and cheaper
whenever traffic has headroom. Nothing here is per-model: the window comes from
the backend and the estimate from a generic tokenizer.

SLACK and MARGIN absorb the gap between the local estimate and vLLM's real
count, so the skip path stays on the safe side (an over-estimate only makes us
tokenize when we did not strictly need to, never the reverse).

Opt-OUT (default): a client that sends `x-honor-max-tokens: true` is passed
through untouched.

Wire up:

    litellm_settings:
      callbacks: ["clamp_smart.proxy_handler_instance"]
"""

import logging
import os
from typing import Any, Optional, Tuple

import httpx

import litellm
from litellm._logging import verbose_proxy_logger
from litellm.integrations.custom_logger import CustomLogger

_LOG_PREFIX = "[clamp_smart]"
_MAX_TOKEN_FIELDS = ("max_tokens", "max_completion_tokens")
_PROVIDER_PREFIXES = ("openai/", "hosted_vllm/", "vllm/", "litellm_proxy/")
FALLBACK_TOKENIZE_URL = os.getenv("VLLM_TOKENIZE_URL")
BYPASS_HEADER = os.getenv("CLAMP_BYPASS_HEADER", "x-honor-max-tokens").lower()
_TRUTHY = {"1", "true", "yes", "on"}
# Headroom the local estimate must clear before we trust it and skip /tokenize.
SLACK = float(os.getenv("CLAMP_SMART_SLACK", "1.3"))
MARGIN = int(os.getenv("CLAMP_SMART_MARGIN", "128"))


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
    if not BYPASS_HEADER:
        return False
    val = _request_headers(data).get(BYPASS_HEADER)
    return val is not None and str(val).lower() in _TRUTHY


def _strip_provider(model: str) -> str:
    for p in _PROVIDER_PREFIXES:
        if model.startswith(p):
            return model[len(p) :]
    return model


def _default_estimate(data: dict, model: Optional[str]) -> Optional[int]:
    try:
        if data.get("messages") is not None:
            return litellm.token_counter(model=model or "", messages=data["messages"])
        if data.get("prompt") is not None:
            return litellm.token_counter(model=model or "", text=data["prompt"])
    except Exception:
        return None
    return None


class ClampSmart(CustomLogger):
    def __init__(
        self, window_info: Optional[Any] = None, estimate: Optional[Any] = None
    ) -> None:
        super().__init__()
        self._client = httpx.AsyncClient(timeout=10.0)
        self._window_info = window_info or self._default_window_info
        self._estimate = estimate or _default_estimate
        self._window_cache: dict[str, int] = {}

    @staticmethod
    def _resolve(model: Optional[str]) -> Optional[Tuple[str, str]]:
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
                return base + "/tokenize", _strip_provider(lp.get("model") or model)
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

    def _fits_without_tokenize(
        self, data: dict, model: Optional[str], requested: int
    ) -> bool:
        window = self._window_cache.get(model or "")
        if window is None:
            return False
        est = self._estimate(data, model)
        if est is None:
            return False
        if est * SLACK + requested + MARGIN <= window:
            verbose_proxy_logger.info(
                "%s model=%s est=%s window=%s requested=%s -> FITS, skipped tokenize",
                _LOG_PREFIX,
                model,
                est,
                window,
                requested,
            )
            return True
        return False

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

        if self._fits_without_tokenize(data, model, requested):
            return data

        info = await self._window_info(data, model)
        if info is None:
            return data
        prompt_tokens, max_model_len = info
        self._window_cache[model or ""] = max_model_len
        available = max_model_len - prompt_tokens
        if available <= 0:
            verbose_proxy_logger.info(
                "%s model=%s prompt_tokens=%s max_model_len=%s -> PROMPT OVERFLOWS, "
                "unchanged (will 400)",
                _LOG_PREFIX,
                model,
                prompt_tokens,
                max_model_len,
            )
            return data
        if requested > available:
            data[field] = available
            verbose_proxy_logger.info(
                "%s model=%s prompt_tokens=%s max_model_len=%s -> CLAMPED %s %s -> %s",
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
                "%s model=%s prompt_tokens=%s max_model_len=%s available=%s -> HONOURED (fits)",
                _LOG_PREFIX,
                model,
                prompt_tokens,
                max_model_len,
                available,
            )
        return data


proxy_handler_instance = ClampSmart()
