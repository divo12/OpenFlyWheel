from collections.abc import Awaitable
from typing import Protocol

class ASGIApp(Protocol):
    def __call__(self, scope: object, receive: object, send: object) -> Awaitable[None]: ...

def run(
    app: ASGIApp,
    *,
    host: str = ...,
    port: int = ...,
    log_level: str | None = ...,
) -> None: ...
