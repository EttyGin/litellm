"""
Default-tag normalizer hook.

Rule: any deployment that carries no tags is given tags: ["default"], so a
tagged request (developer/key with a tag) can still reach it via LiteLLM's
native default-deployment fallback. The request's own tags are never touched,
so tag based spend-logging / budgets keep working.

Runs in async_pre_call_hook. To avoid re-scanning the whole model list on every
request, it only walks the list when the number of deployments changed since the
last pass (a single len() compare in steady state). That still catches models
added at runtime via /model/new, without hooking the model-add path (LiteLLM
exposes no such callback) or monkeypatching router internals.
"""

from typing import Literal, Optional

from litellm.integrations.custom_logger import CustomLogger


class DefaultTagNormalizer(CustomLogger):
    _last_count = -1

    def _normalize(self, llm_router) -> None:
        model_list = llm_router.model_list or []
        if len(model_list) == self.__class__._last_count:
            return  # no model added/removed since last pass -> skip the walk
        for dep in model_list:
            params = dep.setdefault("litellm_params", {})
            if not params.get("tags"):
                params["tags"] = ["default"]
        self.__class__._last_count = len(model_list)

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
