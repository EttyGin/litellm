"""
Auto-loaded at interpreter startup (PYTHONPATH=/patch). Its only job is to
pull in the runtime patches; each patch lives in its own module and
self-installs on import:

  - maxtokens_debug        — max-tokens debug logging   (env CUSTOM_DEBUG)
  - empty_content_guard    — empty /v1/messages guard    (env EMPTY_CONTENT_GUARD)

Add a new patch by dropping a module next to this file and importing it here.
"""
import maxtokens_debug  # noqa: F401
import empty_content_guard  # noqa: F401
