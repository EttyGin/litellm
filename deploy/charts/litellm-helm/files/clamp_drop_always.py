"""Version 1 — ALWAYS drop max_tokens. Fully generic, zero latency, no config.

Why this always works for ANY model with ZERO assumptions:
  * It computes nothing locally and calls nothing — it just removes `max_tokens`
    (and `max_completion_tokens`) from the request.
  * The backend (vLLM / any OpenAI-compatible server) then generates up to
    `max_model_len - prompt_tokens` and stops at EOS. PROVEN: with no max_tokens
    and a rambling prompt, vLLM returned finish_reason="length", prompt=51,
    completion=49, total=100 (exactly the window).
  * So there is never an output-overflow 400. An over-long *prompt* still errors
    at the backend (the one case meant to fail).
  * No window needed, no token count needed, no per-model `model_info`, no magic
    number/margin. Works for every model the same way.

Trade-off: `max_tokens` is never honoured as a cap — output is bounded by the
context window and the model's natural stop, not the requested number.

Opt-OUT (default): the hook drops max_tokens for EVERY chat request, UNLESS the
client explicitly asks us to honour their max_tokens by sending the header
  x-honor-max-tokens: true
The header name is configurable via CLAMP_BYPASS_HEADER (default
"x-honor-max-tokens"); set "" to disable opt-out entirely.

Wire up via litellm config:

    litellm_settings:
      callbacks: ["clamp_drop_always.proxy_handler_instance"]
"""
import logging
import os
from typing import Any, Optional

from litellm._logging import verbose_proxy_logger
from litellm.integrations.custom_logger import CustomLogger

_CHAT_CALLS = {"completion", "acompletion"}
_LOG_PREFIX = "[clamp_drop_always]"
# A client uses this header to explicitly opt OUT (= "honour my max_tokens").
BYPASS_HEADER = os.getenv("CLAMP_BYPASS_HEADER", "x-honor-max-tokens").lower()
_TRUTHY = {"1", "true", "yes", "on"}


def _ensure_info_level() -> None:
    if verbose_proxy_logger.getEffectiveLevel() > logging.INFO:
        verbose_proxy_logger.setLevel(logging.INFO)


def client_opted_out(data: dict) -> bool:
    """True if the client sent the opt-out header to HONOUR their max_tokens."""
    if not BYPASS_HEADER:
        return False
    md = data.get("metadata") or {}
    headers = md.get("headers") or data.get("headers") or {}
    val = headers.get(BYPASS_HEADER)  # header names arrive lower-cased
    return val is not None and str(val).lower() in _TRUTHY


class ClampDropAlways(CustomLogger):
    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict,
        call_type: str,
    ) -> Optional[dict]:
        if call_type not in _CHAT_CALLS:
            return data
        _ensure_info_level()
        if data.get("max_tokens") is None and data.get("max_completion_tokens") is None:
            return data
        if client_opted_out(data):
            verbose_proxy_logger.info(
                "%s model=%s -> client opted out (honour max_tokens), unchanged",
                _LOG_PREFIX, data.get("model"),
            )
            return data
        requested = data.get("max_tokens") or data.get("max_completion_tokens")
        data.pop("max_tokens", None)
        data.pop("max_completion_tokens", None)
        verbose_proxy_logger.info(
            "%s model=%s requested_max_tokens=%s -> DROPPED (backend fills "
            "remaining window)",
            _LOG_PREFIX, data.get("model"), requested,
        )
        return data


proxy_handler_instance = ClampDropAlways()
