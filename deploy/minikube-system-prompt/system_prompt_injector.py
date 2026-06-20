import json
import os
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

from litellm._logging import verbose_proxy_logger
from litellm.integrations.custom_logger import CustomLogger
from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices

DEFAULT_INSTRUCTION = (
    "You are a deployment-managed assistant. Always answer helpfully and "
    "never return an empty message; if you have nothing else to say, briefly "
    "acknowledge the request."
)
DEFAULT_EMPTY_RESPONSE_FALLBACK = "I wasn't able to produce a response for that request. Could you rephrase or add detail?"

CHAT_CALL_TYPES = frozenset({"completion", "acompletion"})
ANTHROPIC_MESSAGES_CALL_TYPE = "anthropic_messages"


class SystemPromptInjector(CustomLogger):
    def __init__(
        self,
        instruction: Optional[str] = None,
        empty_response_fallback: Optional[str] = None,
        max_tokens_caps: Optional[Dict[str, int]] = None,
        default_max_tokens_cap: Optional[int] = None,
    ) -> None:
        self._configured_instruction = instruction or DEFAULT_INSTRUCTION
        self._empty_response_fallback = (
            empty_response_fallback or DEFAULT_EMPTY_RESPONSE_FALLBACK
        )
        env_caps, env_default = self._caps_from_env()
        self._max_tokens_caps = (
            max_tokens_caps if max_tokens_caps is not None else env_caps
        )
        self._default_max_tokens_cap = (
            default_max_tokens_cap
            if default_max_tokens_cap is not None
            else env_default
        )

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
        if call_type in CHAT_CALL_TYPES:
            if not isinstance(data.get("messages"), list):
                return None
            self._inject_chat_system(data, call_type)
        elif call_type == ANTHROPIC_MESSAGES_CALL_TYPE:
            self._inject_anthropic_system(data, call_type)
        else:
            return None

        self._enforce_max_tokens(data)
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
    ) -> AsyncGenerator[Any, None]:
        saw_openai_chunk = False
        produced_output = False
        last_chunk: Optional[ModelResponseStream] = None
        async for chunk in response:
            if hasattr(chunk, "choices"):
                saw_openai_chunk = True
                if not produced_output and self._chunk_has_output(chunk):
                    produced_output = True
            last_chunk = chunk
            yield chunk

        if saw_openai_chunk and not produced_output:
            yield self._build_fallback_chunk(last_chunk)

    def _inject_chat_system(self, data: dict, call_type: str) -> None:
        messages = data["messages"]
        instruction = self.instruction
        if self._chat_already_injected(messages, instruction):
            return
        messages.insert(0, {"role": "system", "content": instruction})
        verbose_proxy_logger.info(
            "system_prompt_injector: prepended fixed system prompt (call_type=%s, messages=%d) -> %r",
            call_type,
            len(messages),
            instruction,
        )

    def _inject_anthropic_system(self, data: dict, call_type: str) -> None:
        instruction = self.instruction
        system = data.get("system")
        if isinstance(system, list):
            if self._anthropic_blocks_injected(system, instruction):
                return
            data["system"] = [{"type": "text", "text": instruction}, *system]
        elif isinstance(system, str) and system != "":
            if system == instruction or system.startswith(instruction + "\n\n"):
                return
            data["system"] = instruction + "\n\n" + system
        else:
            data["system"] = instruction

        verbose_proxy_logger.info(
            "system_prompt_injector: set fixed system prompt (call_type=%s) -> %r",
            call_type,
            instruction,
        )

    def _enforce_max_tokens(self, data: dict) -> None:
        cap = self._max_tokens_cap(data.get("model"))
        if cap is None:
            return
        current = data.get("max_tokens")
        if current is None or current > cap:
            data["max_tokens"] = cap
            verbose_proxy_logger.info(
                "system_prompt_injector: enforced max_tokens cap (model=%s, requested=%s) -> %d",
                data.get("model"),
                current,
                cap,
            )

    def _max_tokens_cap(self, model: Optional[str]) -> Optional[int]:
        if model is not None and model in self._max_tokens_caps:
            return self._max_tokens_caps[model]
        return self._default_max_tokens_cap

    @staticmethod
    def _caps_from_env() -> Tuple[Dict[str, int], Optional[int]]:
        caps: Dict[str, int] = {}
        raw = os.getenv("LITELLM_MAX_TOKENS_CAPS")
        if raw:
            caps = {model: int(value) for model, value in json.loads(raw).items()}
        default_raw = os.getenv("LITELLM_MAX_TOKENS_DEFAULT_CAP")
        default = int(default_raw) if default_raw else None
        return caps, default

    @staticmethod
    def _chat_already_injected(messages: List[Any], instruction: str) -> bool:
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "system":
                continue
            if message.get("content") == instruction:
                return True
        return False

    @staticmethod
    def _anthropic_blocks_injected(blocks: List[Any], instruction: str) -> bool:
        for block in blocks:
            if (
                isinstance(block, dict)
                and block.get("type") == "text"
                and block.get("text") == instruction
            ):
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
        if getattr(message, "tool_calls", None) or getattr(
            message, "function_call", None
        ):
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
            if getattr(delta, "tool_calls", None) or getattr(
                delta, "function_call", None
            ):
                return True
            content = getattr(delta, "content", None)
            if isinstance(content, str) and content != "":
                return True
        return False

    def _build_fallback_chunk(
        self, template: Optional[ModelResponseStream]
    ) -> ModelResponseStream:
        return ModelResponseStream(
            model=getattr(template, "model", None),
            choices=[
                StreamingChoices(
                    index=0,
                    delta=Delta(
                        role="assistant", content=self._empty_response_fallback
                    ),
                    finish_reason="stop",
                )
            ],
        )


injector_instance = SystemPromptInjector()
