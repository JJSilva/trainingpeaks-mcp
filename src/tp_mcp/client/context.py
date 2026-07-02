"""Context variables scoped to a single in-flight tool call / request."""

import contextvars

athlete_override: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "athlete_override", default=None
)

# Opaque OAuth subject for the authenticated user of the current request.
# Set by the HTTP bearer-auth layer (via server.call_tool) before dispatching a
# tool call in multi-user mode. ``None`` means the single-user/local path:
# credentials come from env / keyring / encrypted file exactly as before.
current_subject: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_subject", default=None
)
