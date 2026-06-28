import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[3]
    / "deploy"
    / "charts"
    / "litellm-helm"
    / "files"
    / "clamp_generic_exact.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("clamp_generic_exact", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


module = _load_module()
ClampGenericExact = module.ClampGenericExact
client_opted_out = module.client_opted_out


def _fixed_window(prompt_tokens, max_model_len):
    async def _window_info(data, model):
        return (prompt_tokens, max_model_len)

    return _window_info


def _hook(prompt_tokens=34, max_model_len=100):
    return ClampGenericExact(window_info=_fixed_window(prompt_tokens, max_model_len))


async def _pre_call(hook, data, call_type):
    return await hook.async_pre_call_hook(
        user_api_key_dict=None, cache=None, data=data, call_type=call_type
    )


def _msg(max_field="max_tokens", value=1_000_000, **extra):
    data = {"model": "qwen-0.5b", "messages": [{"role": "user", "content": "hi"}]}
    if max_field is not None:
        data[max_field] = value
    data.update(extra)
    return data


@pytest.mark.asyncio
async def test_clamps_anthropic_messages_call_type():
    """Regression: /v1/messages runs as call_type='anthropic_messages'.

    The original hook gated on {'completion', 'acompletion'} and skipped this
    path entirely, so over-window requests reached vLLM and 400'd.
    """
    data = _msg("max_tokens", 1_000_000)
    out = await _pre_call(_hook(34, 100), data, "anthropic_messages")
    assert out["max_tokens"] == 66  # max_model_len - prompt_tokens


@pytest.mark.asyncio
async def test_clamps_max_completion_tokens_field():
    data = _msg("max_completion_tokens", 999_999)
    out = await _pre_call(_hook(34, 100), data, "acompletion")
    assert out["max_completion_tokens"] == 66
    assert "max_tokens" not in out


@pytest.mark.asyncio
async def test_honours_when_request_fits():
    data = _msg("max_tokens", 50)  # available = 100 - 34 = 66, 50 < 66
    out = await _pre_call(_hook(34, 100), data, "anthropic_messages")
    assert out["max_tokens"] == 50


@pytest.mark.asyncio
async def test_prompt_overflow_left_untouched_to_fail_at_backend():
    data = _msg("max_tokens", 1_000)
    out = await _pre_call(_hook(120, 100), data, "anthropic_messages")  # available < 0
    assert out["max_tokens"] == 1_000


@pytest.mark.asyncio
async def test_request_without_max_token_field_passes_through():
    data = _msg(max_field=None)
    out = await _pre_call(_hook(34, 100), data, "anthropic_messages")
    assert "max_tokens" not in out and "max_completion_tokens" not in out


@pytest.mark.parametrize(
    "data",
    [
        {"metadata": {"headers": {"x-honor-max-tokens": "true"}}},
        {"litellm_metadata": {"headers": {"x-honor-max-tokens": "true"}}},
        {"proxy_server_request": {"headers": {"x-honor-max-tokens": "true"}}},
        {"headers": {"x-honor-max-tokens": "true"}},
    ],
)
def test_opt_out_header_detected_in_every_location(data):
    assert client_opted_out(data) is True


def test_opt_out_absent_by_default():
    assert client_opted_out({"metadata": {"headers": {"user-agent": "x"}}}) is False
    assert client_opted_out({}) is False


@pytest.mark.asyncio
async def test_opt_out_header_via_litellm_metadata_skips_clamp():
    """Regression: on /v1/messages the opt-out header lives under
    litellm_metadata, not metadata; the original check missed it and clamped
    anyway."""
    data = _msg(
        "max_tokens",
        1_000_000,
        litellm_metadata={"headers": {"x-honor-max-tokens": "true"}},
    )
    out = await _pre_call(_hook(34, 100), data, "anthropic_messages")
    assert out["max_tokens"] == 1_000_000  # untouched -> backend enforces the cap
