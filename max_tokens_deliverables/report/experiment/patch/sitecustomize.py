"""Loaded automatically at Python startup when /exp/patch is on PYTHONPATH.

Two responsibilities, so the ONLY difference between the unpatched and patched
runs is the monkey patch itself:

  1. Always register the synthetic "bug-model" into litellm.model_cost with
     equal input/output limits (8192/8192). This is what makes the faulty Case 1
     heuristic fire at all; it is identical in both runs.

  2. Install the max_tokens passthrough ONLY when APPLY_PATCH=1.
"""
import os

try:
    import litellm
    litellm.register_model({
        "openai/bug-model": {
            "max_tokens": 8192,
            "max_input_tokens": 8192,
            "max_output_tokens": 8192,
            "litellm_provider": "openai",
            "mode": "chat",
        }
    })
    print("[setup] registered openai/bug-model (8192/8192) into model_cost", flush=True)
except Exception as e:  # pragma: no cover
    print(f"[setup] register failed: {e}", flush=True)

if os.environ.get("APPLY_PATCH") == "1":
    try:
        import litellm._lazy_imports as _li

        def _passthrough_max_tokens(*, user_max_tokens=None, **kwargs):
            return user_max_tokens

        _li._get_modified_max_tokens_func = _passthrough_max_tokens
        print("[PATCH] max_tokens passthrough installed", flush=True)
    except Exception as e:  # pragma: no cover
        print(f"[PATCH] failed: {e}", flush=True)
else:
    print("[PATCH] not applied (APPLY_PATCH != 1) - baseline buggy behavior", flush=True)
