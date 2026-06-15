"""Disable LiteLLM's automatic max_tokens shrinking (the get_modified_max_tokens
Case 1 bug) without touching modify_params or any other behavior.

Loaded automatically by CPython at interpreter startup when its directory is on
PYTHONPATH. Replaces the lazily-cached rewrite function with a passthrough.
"""
import litellm._lazy_imports as _li


def _passthrough_max_tokens(*, user_max_tokens=None, **kwargs):
    return user_max_tokens


_li._get_modified_max_tokens_func = _passthrough_max_tokens
print("[litellm-patch] max_tokens passthrough installed", flush=True)
