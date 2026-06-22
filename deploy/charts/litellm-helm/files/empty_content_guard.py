"""
Empty-content guard for the Anthropic `/v1/messages` bridge path.

Self-installs on import (gated by env EMPTY_CONTENT_GUARD, default on). Loaded
at interpreter startup because `sitecustomize.py` imports it and /patch is on
PYTHONPATH — so the patch runs before the proxy boots, no image rebuild needed.

Problem it closes: some reasoning models/providers place the whole answer in
`reasoning_content` and leave `content` empty (finish_reason="stop", unrelated
to max_tokens) — e.g. a native reasoning field, or a "<think>...</think>" body
with no text after the closing tag. The /v1/messages bridge adapter then emits
a `thinking` block only, so the client sees an empty turn and the agentic loop
stalls.

Guard: when the assembled Anthropic content has no `text` and no `tool_use`
block BUT there is reasoning, surface that reasoning as a `text` block so the
client sees the model's real output. A truly-empty response (no reasoning
either) is left untouched. Mirrors the source fix in transformation.py.
"""
import os

_GUARD = os.getenv("EMPTY_CONTENT_GUARD", "1").strip().lower() in ("1", "true", "yes", "on")
_KEEP_TYPES = ("text", "tool_use")


def _block_type(block):
    if isinstance(block, dict):
        return block.get("type")
    return getattr(block, "type", None)


def _block_thinking(block):
    if isinstance(block, dict):
        return block.get("thinking")
    return getattr(block, "thinking", None)


def install():
    from litellm.llms.anthropic.experimental_pass_through.adapters.transformation import (
        LiteLLMAnthropicMessagesAdapter,
    )

    _original = LiteLLMAnthropicMessagesAdapter.translate_openai_response_to_anthropic

    def _guarded(self, response, tool_name_mapping=None):
        result = _original(self, response, tool_name_mapping=tool_name_mapping)

        content = result.get("content") if isinstance(result, dict) else getattr(result, "content", None)
        if isinstance(content, list) and not any(
            _block_type(b) in _KEEP_TYPES for b in content
        ):
            fallback = "".join(
                _block_thinking(b) or "" for b in content if _block_type(b) == "thinking"
            )
            # Only act when content is empty, tool_use is empty AND there is
            # reasoning to surface. A truly-empty response is left untouched.
            if fallback:
                content.append({"type": "text", "text": fallback})
                print(
                    "[empty-content-guard] surfaced reasoning as text "
                    f"({len(fallback)} chars; response had no text/tool_use)",
                    flush=True,
                )
        return result

    LiteLLMAnthropicMessagesAdapter.translate_openai_response_to_anthropic = _guarded
    print("[empty-content-guard] installed", flush=True)


if _GUARD:
    try:
        install()
    except Exception as e:  # never block proxy startup on the guard
        print(f"[empty-content-guard] failed to install: {e}", flush=True)
