"""Only the owner's devices may use Jarvis remotely (via ``tailscale serve``).

The server keeps listening on 127.0.0.1; ``tailscale serve`` proxies HTTPS
requests from the user's tailnet and adds ``Tailscale-User-Login`` with the
authenticated user (overwriting any client-supplied value). Requests that carry
that header must belong to an allowed login — remote access is closed unless
``[server] remote_allowed_logins`` lists someone. Local requests (no header)
behave exactly as before.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

HEADER = b"tailscale-user-login"


class TailnetGuard:
    """Pure ASGI middleware, so it also covers WebSocket upgrades."""

    def __init__(self, app: Any, allowed_logins: Iterable[str] = ()) -> None:
        self.app = app
        self.allowed = {a.strip().lower() for a in allowed_logins if a and a.strip()}

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") in ("http", "websocket"):
            login = None
            for key, value in scope.get("headers") or []:
                if key.lower() == HEADER:
                    login = value.decode("utf-8", "replace").strip().lower()
                    break
            if login is not None and login not in self.allowed:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                body = json.dumps(
                    {"detail": "Acesso remoto não autorizado para esta conta."}
                ).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def allowed_from_config(config: Any) -> list[str]:
    raw = getattr(getattr(config, "server", None), "remote_allowed_logins", "") or ""
    if isinstance(raw, str):
        raw = raw.split(",")
    return [str(x).strip() for x in raw if str(x).strip()]
