from collections.abc import Awaitable, Callable
from typing import TypeVar

F = TypeVar("F", bound=Callable[..., object])

class FastAPI:
    def __init__(
        self,
        *,
        title: str = ...,
        docs_url: str | None = ...,
        openapi_url: str | None = ...,
        redoc_url: str | None = ...,
    ) -> None: ...
    def get(
        self,
        path: str,
        **kwargs: object,
    ) -> Callable[[F], F]: ...
    def add_api_route(
        self,
        path: str,
        endpoint: Callable[..., object],
        *,
        methods: list[str] | None = ...,
        **kwargs: object,
    ) -> None: ...
    def __call__(self, scope: object, receive: object, send: object) -> Awaitable[None]: ...

def Header(default: object = ..., *, alias: str | None = ...) -> str | None: ...

class HTTPException(Exception):
    def __init__(self, *, status_code: int, detail: str) -> None: ...
