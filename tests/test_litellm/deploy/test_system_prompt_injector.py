import importlib.util
from pathlib import Path

import pytest

from litellm.types.utils import (
    Choices,
    Delta,
    Message,
    ModelResponse,
    ModelResponseStream,
    StreamingChoices,
)

MODULE_PATH = (
    Path(__file__).resolve().parents[3]
    / "deploy"
    / "minikube-system-prompt"
    / "system_prompt_injector.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("system_prompt_injector", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


module = _load_module()
SystemPromptInjector = module.SystemPromptInjector
DEFAULT_INSTRUCTION = module.DEFAULT_INSTRUCTION
DEFAULT_EMPTY_RESPONSE_FALLBACK = module.DEFAULT_EMPTY_RESPONSE_FALLBACK


@pytest.fixture
def injector():
    return SystemPromptInjector(instruction="FIXED INSTRUCTION")


async def _pre_call(injector, data, call_type="acompletion"):
    return await injector.async_pre_call_hook(
        user_api_key_dict=None, cache=None, data=data, call_type=call_type
    )


@pytest.mark.asyncio
async def test_prepends_system_message(injector):
    data = {"messages": [{"role": "user", "content": "hi"}]}
    result = await _pre_call(injector, data)
    assert result["messages"][0] == {"role": "system", "content": "FIXED INSTRUCTION"}
    assert result["messages"][1] == {"role": "user", "content": "hi"}


@pytest.mark.asyncio
async def test_preserves_existing_messages_verbatim(injector):
    original = [
        {"role": "system", "content": "caller system prompt"},
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    data = {"messages": [dict(m) for m in original]}
    result = await _pre_call(injector, data)
    assert result["messages"][1:] == original
    assert result["messages"][0]["role"] == "system"
    assert result["messages"][0]["content"] == "FIXED INSTRUCTION"


@pytest.mark.asyncio
async def test_logs_injection_at_info_level(injector, caplog):
    import logging

    data = {"messages": [{"role": "user", "content": "hi"}]}
    with caplog.at_level(logging.INFO, logger="LiteLLM Proxy"):
        await _pre_call(injector, data)
    assert any(
        "system_prompt_injector: prepended fixed system prompt" in r.message
        for r in caplog.records
    )


@pytest.mark.asyncio
async def test_no_log_when_already_injected(injector, caplog):
    import logging

    data = {"messages": [{"role": "user", "content": "hi"}]}
    await _pre_call(injector, data)
    with caplog.at_level(logging.INFO, logger="LiteLLM Proxy"):
        await _pre_call(injector, data)
    assert not any(
        "system_prompt_injector: prepended" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_idempotent_no_double_injection(injector):
    data = {"messages": [{"role": "user", "content": "hi"}]}
    await _pre_call(injector, data)
    await _pre_call(injector, data)
    system_messages = [m for m in data["messages"] if m["role"] == "system"]
    assert len(system_messages) == 1


@pytest.mark.asyncio
async def test_skips_non_chat_call_types(injector):
    data = {"input": "embed me"}
    result = await _pre_call(injector, data, call_type="aembedding")
    assert result is None


@pytest.mark.asyncio
async def test_skips_when_messages_missing(injector):
    data = {"messages": "not-a-list"}
    result = await _pre_call(injector, data)
    assert result is None


@pytest.mark.asyncio
async def test_env_var_overrides_instruction(injector, monkeypatch):
    monkeypatch.setenv("LITELLM_SYSTEM_PROMPT", "ENV INSTRUCTION")
    data = {"messages": [{"role": "user", "content": "hi"}]}
    result = await _pre_call(injector, data)
    assert result["messages"][0]["content"] == "ENV INSTRUCTION"


@pytest.mark.asyncio
async def test_anthropic_sets_system_when_absent(injector):
    data = {"messages": [{"role": "user", "content": "hi"}]}
    result = await _pre_call(injector, data, call_type="anthropic_messages")
    assert result["system"] == "FIXED INSTRUCTION"
    assert result["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_anthropic_prepends_to_string_system(injector):
    data = {"system": "caller system", "messages": []}
    result = await _pre_call(injector, data, call_type="anthropic_messages")
    assert result["system"] == "FIXED INSTRUCTION\n\ncaller system"


@pytest.mark.asyncio
async def test_anthropic_prepends_text_block_to_list_system(injector):
    blocks = [{"type": "text", "text": "caller block"}]
    data = {"system": [dict(b) for b in blocks], "messages": []}
    result = await _pre_call(injector, data, call_type="anthropic_messages")
    assert result["system"][0] == {"type": "text", "text": "FIXED INSTRUCTION"}
    assert result["system"][1:] == blocks


@pytest.mark.asyncio
async def test_anthropic_idempotent_string(injector):
    data = {"system": "caller", "messages": []}
    await _pre_call(injector, data, call_type="anthropic_messages")
    await _pre_call(injector, data, call_type="anthropic_messages")
    assert data["system"] == "FIXED INSTRUCTION\n\ncaller"


@pytest.mark.asyncio
async def test_anthropic_idempotent_list(injector):
    data = {"system": [{"type": "text", "text": "caller block"}], "messages": []}
    await _pre_call(injector, data, call_type="anthropic_messages")
    await _pre_call(injector, data, call_type="anthropic_messages")
    text_blocks = [b for b in data["system"] if b.get("text") == "FIXED INSTRUCTION"]
    assert len(text_blocks) == 1


@pytest.mark.asyncio
async def test_anthropic_never_injects_system_role_into_messages(injector):
    data = {"messages": [{"role": "user", "content": "hi"}]}
    result = await _pre_call(injector, data, call_type="anthropic_messages")
    assert all(m["role"] != "system" for m in result["messages"])


@pytest.fixture
def capped_injector():
    return SystemPromptInjector(
        instruction="FIXED INSTRUCTION",
        max_tokens_caps={"mock-gpt": 1024},
        default_max_tokens_cap=256,
    )


@pytest.mark.asyncio
async def test_max_tokens_clamped_when_request_exceeds_per_model_cap(capped_injector):
    data = {
        "model": "mock-gpt",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 9999,
    }
    result = await _pre_call(capped_injector, data)
    assert result["max_tokens"] == 1024


@pytest.mark.asyncio
async def test_max_tokens_defaulted_when_request_omits_it(capped_injector):
    data = {"model": "mock-gpt", "messages": [{"role": "user", "content": "hi"}]}
    result = await _pre_call(capped_injector, data)
    assert result["max_tokens"] == 1024


@pytest.mark.asyncio
async def test_max_tokens_preserved_when_below_cap(capped_injector):
    data = {
        "model": "mock-gpt",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
    }
    result = await _pre_call(capped_injector, data)
    assert result["max_tokens"] == 100


@pytest.mark.asyncio
async def test_max_tokens_uses_default_cap_for_unlisted_model(capped_injector):
    data = {
        "model": "other-model",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 9999,
    }
    result = await _pre_call(capped_injector, data)
    assert result["max_tokens"] == 256


@pytest.mark.asyncio
async def test_max_tokens_untouched_when_no_cap_configured(injector):
    data = {
        "model": "mock-gpt",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 9999,
    }
    result = await _pre_call(injector, data)
    assert result["max_tokens"] == 9999


@pytest.mark.asyncio
async def test_max_tokens_enforced_on_anthropic_messages(capped_injector):
    data = {
        "model": "mock-gpt",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 9999,
    }
    result = await _pre_call(capped_injector, data, call_type="anthropic_messages")
    assert result["max_tokens"] == 1024


def test_caps_loaded_from_env(monkeypatch):
    monkeypatch.setenv("LITELLM_MAX_TOKENS_CAPS", '{"mock-gpt": 512}')
    monkeypatch.setenv("LITELLM_MAX_TOKENS_DEFAULT_CAP", "128")
    injector = SystemPromptInjector(instruction="FIXED INSTRUCTION")
    assert injector._max_tokens_cap("mock-gpt") == 512
    assert injector._max_tokens_cap("unlisted") == 128


@pytest.fixture
def delta_injector():
    return SystemPromptInjector(instruction="FIXED INSTRUCTION", max_tokens_delta=10)


@pytest.mark.asyncio
async def test_max_tokens_reduced_by_delta(delta_injector):
    data = {
        "model": "mock-gpt",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
    }
    result = await _pre_call(delta_injector, data)
    assert result["max_tokens"] == 90


@pytest.mark.asyncio
async def test_max_tokens_reduced_by_delta_on_anthropic(delta_injector):
    data = {
        "model": "mock-gpt",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 50,
    }
    result = await _pre_call(delta_injector, data, call_type="anthropic_messages")
    assert result["max_tokens"] == 40


@pytest.mark.asyncio
async def test_max_tokens_delta_floors_at_one(delta_injector):
    data = {
        "model": "mock-gpt",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 6,
    }
    result = await _pre_call(delta_injector, data)
    assert result["max_tokens"] == 1


@pytest.mark.asyncio
async def test_max_tokens_delta_noop_when_request_omits_it(delta_injector):
    data = {"model": "mock-gpt", "messages": [{"role": "user", "content": "hi"}]}
    result = await _pre_call(delta_injector, data)
    assert "max_tokens" not in result


@pytest.mark.asyncio
async def test_max_tokens_no_delta_by_default(injector):
    data = {
        "model": "mock-gpt",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 100,
    }
    result = await _pre_call(injector, data)
    assert result["max_tokens"] == 100


def test_delta_loaded_from_env(monkeypatch):
    monkeypatch.setenv("LITELLM_MAX_TOKENS_DELTA", "10")
    injector = SystemPromptInjector(instruction="FIXED INSTRUCTION")
    assert injector._max_tokens_delta == 10


def _chat_response(content, tool_calls=None):
    message = Message(content=content, role="assistant", tool_calls=tool_calls)
    return ModelResponse(choices=[Choices(index=0, message=message)])


@pytest.mark.asyncio
async def test_post_call_fills_empty_content(injector):
    response = _chat_response(content="")
    result = await injector.async_post_call_success_hook(
        data={}, user_api_key_dict=None, response=response
    )
    assert result.choices[0].message.content == DEFAULT_EMPTY_RESPONSE_FALLBACK


@pytest.mark.asyncio
async def test_post_call_fills_none_content(injector):
    response = _chat_response(content=None)
    result = await injector.async_post_call_success_hook(
        data={}, user_api_key_dict=None, response=response
    )
    assert result.choices[0].message.content == DEFAULT_EMPTY_RESPONSE_FALLBACK


@pytest.mark.asyncio
async def test_post_call_preserves_nonempty_content(injector):
    response = _chat_response(content="real answer")
    result = await injector.async_post_call_success_hook(
        data={}, user_api_key_dict=None, response=response
    )
    assert result.choices[0].message.content == "real answer"


@pytest.mark.asyncio
async def test_post_call_leaves_tool_calls_untouched(injector):
    tool_calls = [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": "{}"},
        }
    ]
    response = _chat_response(content=None, tool_calls=tool_calls)
    result = await injector.async_post_call_success_hook(
        data={}, user_api_key_dict=None, response=response
    )
    assert result.choices[0].message.content in (None, "")
    assert result.choices[0].message.tool_calls


async def _stream(chunks):
    for chunk in chunks:
        yield chunk


def _content_chunk(text):
    return ModelResponseStream(
        choices=[StreamingChoices(index=0, delta=Delta(content=text))]
    )


@pytest.mark.asyncio
async def test_streaming_passthrough_no_fallback_when_content(injector):
    chunks = [_content_chunk("hel"), _content_chunk("lo")]
    out = [
        c
        async for c in injector.async_post_call_streaming_iterator_hook(
            user_api_key_dict=None, response=_stream(chunks), request_data={}
        )
    ]
    assert len(out) == 2
    assert "".join(c.choices[0].delta.content for c in out) == "hello"


@pytest.mark.asyncio
async def test_streaming_emits_fallback_when_empty(injector):
    chunks = [_content_chunk(""), _content_chunk(None)]
    out = [
        c
        async for c in injector.async_post_call_streaming_iterator_hook(
            user_api_key_dict=None, response=_stream(chunks), request_data={}
        )
    ]
    assert len(out) == 3
    assert out[-1].choices[0].delta.content == DEFAULT_EMPTY_RESPONSE_FALLBACK


class _AnthropicChunk:
    def __init__(self, text):
        self.type = "content_block_delta"
        self.text = text


@pytest.mark.asyncio
async def test_streaming_no_fallback_for_non_openai_chunks(injector):
    chunks = [_AnthropicChunk("hi")]
    out = [
        c
        async for c in injector.async_post_call_streaming_iterator_hook(
            user_api_key_dict=None, response=_stream(chunks), request_data={}
        )
    ]
    assert len(out) == 1
    assert out[0] is chunks[0]


@pytest.mark.asyncio
async def test_streaming_no_fallback_when_tool_call_delta(injector):
    tool_delta = Delta(
        tool_calls=[
            {
                "index": 0,
                "id": "call_1",
                "type": "function",
                "function": {"name": "f", "arguments": "{}"},
            }
        ]
    )
    chunk = ModelResponseStream(choices=[StreamingChoices(index=0, delta=tool_delta)])
    out = [
        c
        async for c in injector.async_post_call_streaming_iterator_hook(
            user_api_key_dict=None, response=_stream([chunk]), request_data={}
        )
    ]
    assert len(out) == 1
