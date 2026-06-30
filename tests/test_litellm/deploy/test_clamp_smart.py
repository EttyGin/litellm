import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[3]
    / "deploy"
    / "charts"
    / "litellm-helm"
    / "files"
    / "clamp_smart.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("clamp_smart", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


module = _load_module()
ClampSmart = module.ClampSmart


def _window_info(prompt_tokens, max_model_len, recorder):
    async def fn(data, model):
        recorder.append(model)
        return (prompt_tokens, max_model_len)

    return fn


def _hook(prompt_tokens=34, max_model_len=100, est=10, recorder=None):
    recorder = recorder if recorder is not None else []
    hook = ClampSmart(
        window_info=_window_info(prompt_tokens, max_model_len, recorder),
        estimate=lambda data, model: est,
    )
    return hook, recorder


def _msg(field="max_tokens", value=1_000_000, **extra):
    data = {"model": "qwen-0.5b", "messages": [{"role": "user", "content": "hi"}]}
    data[field] = value
    data.update(extra)
    return data


async def _pre_call(hook, data, call_type="anthropic_messages"):
    return await hook.async_pre_call_hook(None, None, data, call_type)


@pytest.mark.asyncio
async def test_skips_tokenize_when_comfortably_fits():
    hook, calls = _hook(max_model_len=32768, est=10)
    hook._window_cache["qwen-0.5b"] = 32768  # window already learned
    data = _msg("max_tokens", 100)
    out = await _pre_call(hook, data)
    assert out["max_tokens"] == 100  # untouched
    assert calls == []  # /tokenize was NOT called


@pytest.mark.asyncio
async def test_overflow_still_tokenizes_and_clamps():
    hook, calls = _hook(prompt_tokens=34, max_model_len=100, est=10)
    out = await _pre_call(hook, _msg("max_tokens", 1_000_000))
    assert out["max_tokens"] == 66  # 100 - 34, exact
    assert calls == ["qwen-0.5b"]  # tokenize WAS called


@pytest.mark.asyncio
async def test_first_request_tokenizes_then_caches_window():
    hook, calls = _hook(prompt_tokens=34, max_model_len=32768, est=10)
    # cold cache -> must tokenize even though it fits
    await _pre_call(hook, _msg("max_tokens", 100))
    assert calls == ["qwen-0.5b"]
    assert hook._window_cache["qwen-0.5b"] == 32768
    # warm cache -> identical fitting request now skips
    await _pre_call(hook, _msg("max_tokens", 100))
    assert calls == ["qwen-0.5b"]  # still only the first call


@pytest.mark.asyncio
async def test_near_boundary_tokenizes_even_though_it_would_fit():
    """Estimate cannot prove safety near the window, so stay exact."""
    hook, calls = _hook(prompt_tokens=34, max_model_len=100, est=10)
    hook._window_cache["qwen-0.5b"] = 100
    out = await _pre_call(hook, _msg("max_tokens", 50))  # 10*1.3+50+128 > 100
    assert calls == ["qwen-0.5b"]
    assert out["max_tokens"] == 50  # available 66, honoured exactly


@pytest.mark.asyncio
async def test_clamps_max_completion_tokens_field():
    hook, calls = _hook(prompt_tokens=34, max_model_len=100, est=10)
    out = await _pre_call(hook, _msg("max_completion_tokens", 999_999), "acompletion")
    assert out["max_completion_tokens"] == 66
    assert "max_tokens" not in out


@pytest.mark.asyncio
async def test_opt_out_header_skips_everything():
    hook, calls = _hook(max_model_len=100, est=10)
    data = _msg(
        "max_tokens",
        1_000_000,
        litellm_metadata={"headers": {"x-honor-max-tokens": "true"}},
    )
    out = await _pre_call(hook, data)
    assert out["max_tokens"] == 1_000_000
    assert calls == []  # never tokenized


@pytest.mark.asyncio
async def test_prompt_overflow_left_untouched():
    hook, calls = _hook(prompt_tokens=120, max_model_len=100, est=10)
    out = await _pre_call(hook, _msg("max_tokens", 1_000))
    assert out["max_tokens"] == 1_000  # available < 0 -> unchanged


@pytest.mark.asyncio
async def test_request_without_cap_passes_through():
    hook, calls = _hook()
    data = {"model": "qwen-0.5b", "messages": [{"role": "user", "content": "hi"}]}
    out = await _pre_call(hook, data)
    assert "max_tokens" not in out and "max_completion_tokens" not in out
    assert calls == []
