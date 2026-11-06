"""How a request identifies itself to TNS. Shared by every TNS feed.

Design §5.0 and invariant 4. Two credential modes, differing in HTTP verb:

``marker``
    A registered ``tns_marker{...}`` User-Agent on a plain GET.

``bot``
    The bot's ``api_key`` submitted as form data on a POST, with a ``tns_marker``
    naming the bot in the User-Agent.

The auth config is re-validated here, at the point of use, rather than trusted
from startup: the *download* refuses without a credential, so the guard belongs
where the request is built.
"""

from __future__ import annotations

import json

from ..config import AuthConfig

__all__ = ["build_request", "rejection_detail", "tns_marker"]


def tns_marker(auth: AuthConfig) -> str:
    """The ``tns_marker`` User-Agent TNS uses to attribute a request."""
    if auth.mode == "marker":
        return auth.user_agent
    payload = json.dumps(
        {"tns_id": auth.bot_id, "type": "bot", "name": auth.bot_name},
        separators=(", ", ": "),
    )
    return f"tns_marker{payload}"


def build_request(auth: AuthConfig) -> tuple[str, dict[str, str], dict[str, str] | None]:
    """Return ``(method, headers, form_data)`` for the configured auth mode.

    Raises :class:`~tns_mirror_server.errors.AuthNotConfigured` when neither
    credential is configured — there is no anonymous shape to return.
    """
    auth.validate()
    headers = {"User-Agent": tns_marker(auth)}
    if auth.mode == "marker":
        return "GET", headers, None
    return "POST", headers, {"api_key": auth.api_key}


def rejection_detail(auth: AuthConfig) -> str:
    """What to check when TNS answers 401/403 — the commonest failure by far."""
    if auth.mode == "bot":
        return (
            "TNS rejected the bot credentials. Check TNS_API_KEY, "
            "TNS_BOT_ID and TNS_BOT_NAME against Bot Management in your "
            "TNS profile — the id and name must belong to the same bot "
            "as the key."
        )
    return (
        "TNS rejected the marker. Check TNS_USER_AGENT is the "
        "tns_marker for a registered account, copied whole, "
        "including the surrounding braces."
    )
