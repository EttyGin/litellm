import os

import litellm._lazy_imports as _li

_DEBUG = os.getenv("CUSTOM_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")
_original_func = None


def _debug_get_modified_max_tokens(*args, **kwargs):
    global _original_func
    if _original_func is None:
        from litellm.litellm_core_utils.token_counter import get_modified_max_tokens

        _original_func = get_modified_max_tokens

    model = kwargs.get("model")
    user_max_tokens = kwargs.get("user_max_tokens")
    print(
        f"[litellm-maxtokens-debug] ENTER model={model} max_tokens(in)={user_max_tokens}",
        flush=True,
    )
    modified = _original_func(*args, **kwargs)
    print(
        f"[litellm-maxtokens-debug] EXIT  model={model} max_tokens(in)={user_max_tokens} max_tokens(out)={modified}",
        flush=True,
    )
    return modified


if _DEBUG:
    _li._get_modified_max_tokens_func = _debug_get_modified_max_tokens
    print("[litellm-maxtokens-debug] wrapper installed (original logic unchanged)", flush=True)
