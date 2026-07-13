"""
Default-tag normalizer hook.

Rule: any deployment that carries no tags is given tags: ["default"], so a
tagged request (developer/key with a tag) can still reach it via LiteLLM's
native default-deployment fallback. The request's own tags are never touched,
so tag based spend-logging / budgets keep working.

Runs on every request (no init guard) so models added at runtime via /model/new
are normalized on the next request too — no need to hook the model-add path
(LiteLLM exposes no such callback) or monkeypatch router internals.
The write is idempotent, so repeated passes are effectively free.
"""

from typing import Literal, Optional

from litellm.integrations.custom_logger import CustomLogger


class DefaultTagNormalizer(CustomLogger):
    def _normalize(self, llm_router) -> None:
        for dep in llm_router.model_list or []:
            params = dep.setdefault("litellm_params", {})
            if not params.get("tags"):
                params["tags"] = ["default"]

    async def async_pre_call_hook(
        self,
        user_api_key_dict,
        cache,
        data: dict,
        call_type: Literal[
            "completion",
            "text_completion",
            "embeddings",
            "image_generation",
            "moderation",
            "audio_transcription",
        ],
    ) -> Optional[dict]:
        from litellm.proxy.proxy_server import llm_router

        if llm_router is not None:
            self._normalize(llm_router)
        return data


proxy_handler_instance = DefaultTagNormalizer()
