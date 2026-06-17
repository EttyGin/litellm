import os
from typing import Any, AsyncGenerator, List, Optional

from litellm._logging import verbose_proxy_logger
from litellm.integrations.custom_logger import CustomLogger
from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices

DEFAULT_INSTRUCTION = (
    "You are a deployment-managed assistant. Always answer helpfully and "
    "never return an empty message; if you have nothing else to say, briefly "
    "acknowledge the request."
)
DEFAULT_EMPTY_RESPONSE_FALLBACK = (
    "I wasn't able to produce a response for that request. Could you rephrase or add detail?"
)

CHAT_CALL_TYPES = frozenset({"completion", "acompletion"})


class SystemPromptInjector(CustomLogger):
    def __init__(
        self,
        instruction: Optional[str] = None,
        empty_response_fallback: Optional[str] = None,
    ) -> None:
        self._configured_instruction = instruction or DEFAULT_INSTRUCTION
        self._empty_response_fallback = empty_response_fallback or DEFAULT_EMPTY_RESPONSE_FALLBACK

    @property
    def instruction(self) -> str:
        return os.getenv("LITELLM_SYSTEM_PROMPT") or self._configured_instruction

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict,
        call_type: str,
    ) -> Optional[dict]:
        if call_type not in CHAT_CALL_TYPES:
            return None

        messages = data.get("messages")
        if not isinstance(messages, list):
            return None

        instruction = self.instruction
        if not self._already_injected(messages, instruction):
            messages.insert(0, {"role": "system", "content": instruction})
            verbose_proxy_logger.info(
                "system_prompt_injector: prepended fixed system prompt (call_type=%s, messages=%d) -> %r",
                call_type,
                len(messages),
                instruction,
            )

        return data

    async def async_post_call_success_hook(
        self,
        data: dict,
        user_api_key_dict: Any,
        response: Any,
    ) -> Any:
        for message in self._assistant_messages(response):
            if self._is_empty_turn(message):
                message.content = self._empty_response_fallback
        return response

    async def async_post_call_streaming_iterator_hook(
        self,
        user_api_key_dict: Any,
        response: Any,
        request_data: dict,
    ) -> AsyncGenerator[ModelResponseStream, None]:
        produced_output = False
        last_chunk: Optional[ModelResponseStream] = None
        async for chunk in response:
            if not produced_output and self._chunk_has_output(chunk):
                produced_output = True
            last_chunk = chunk
            yield chunk

        if not produced_output:
            yield self._build_fallback_chunk(last_chunk)

    @staticmethod
    def _already_injected(messages: List[Any], instruction: str) -> bool:
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "system":
                continue
            if message.get("content") == instruction:
                return True
        return False

    @staticmethod
    def _assistant_messages(response: Any) -> List[Any]:
        choices = getattr(response, "choices", None)
        if not choices:
            return []
        messages = []
        for choice in choices:
            message = getattr(choice, "message", None)
            if message is not None:
                messages.append(message)
        return messages

    @staticmethod
    def _is_empty_turn(message: Any) -> bool:
        if getattr(message, "tool_calls", None) or getattr(message, "function_call", None):
            return False
        content = getattr(message, "content", None)
        if content is None:
            return True
        return isinstance(content, str) and content.strip() == ""

    @staticmethod
    def _chunk_has_output(chunk: Any) -> bool:
        for choice in getattr(chunk, "choices", None) or []:
            delta = getattr(choice, "delta", None)
            if delta is None:
                continue
            if getattr(delta, "tool_calls", None) or getattr(delta, "function_call", None):
                return True
            content = getattr(delta, "content", None)
            if isinstance(content, str) and content != "":
                return True
        return False

    def _build_fallback_chunk(self, template: Optional[ModelResponseStream]) -> ModelResponseStream:
        return ModelResponseStream(
            model=getattr(template, "model", None),
            choices=[
                StreamingChoices(
                    index=0,
                    delta=Delta(role="assistant", content=self._empty_response_fallback),
                    finish_reason="stop",
                )
            ],
        )


class FinishReasonLogger(CustomLogger):
    @staticmethod
    def _emit(response: Any) -> None:
        if os.getenv("CUSTOM_DEBUG", "").strip().lower() not in ("1", "true", "yes", "on"):
            return
        model = getattr(response, "model", None)
        for choice in getattr(response, "choices", None) or []:
            finish_reason = getattr(choice, "finish_reason", None)
            print(
                f"[litellm-maxtokens-debug] RESULT model={model} finish_reason={finish_reason}",
                flush=True,
            )

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._emit(response_obj)

    def log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._emit(response_obj)


injector_instance = SystemPromptInjector()
finish_reason_logger = FinishReasonLogger()
