from collections.abc import Awaitable, Callable
from typing import Generic, TypeVar

from mcp_types import CallToolRequestParams, CallToolResult, ListToolsResult, PaginatedRequestParams

LifespanResultT = TypeVar("LifespanResultT", default=object)
ServerRequestContextT = TypeVar("ServerRequestContextT")

class Server(Generic[LifespanResultT]):
    def __init__(
        self,
        name: str,
        *,
        version: str = ...,
        title: str | None = ...,
        description: str | None = ...,
        instructions: str | None = ...,
        website_url: str | None = ...,
        icons: list[object] | None = ...,
        cache_hints: dict[str, object] | None = ...,
        lifespan: Callable[..., object] = ...,
        on_list_tools: Callable[
            [object, PaginatedRequestParams | None],
            Awaitable[ListToolsResult],
        ]
        | None = ...,
        on_call_tool: Callable[
            [object, CallToolRequestParams],
            Awaitable[CallToolResult],
        ]
        | None = ...,
        on_list_resources: Callable[..., Awaitable[object]] | None = ...,
        on_list_resource_templates: Callable[..., Awaitable[object]] | None = ...,
        on_read_resource: Callable[..., Awaitable[object]] | None = ...,
        on_subscribe_resource: Callable[..., Awaitable[object]] | None = ...,
        on_unsubscribe_resource: Callable[..., Awaitable[object]] | None = ...,
        on_subscriptions_listen: Callable[..., Awaitable[object]] | None = ...,
        on_list_prompts: Callable[..., Awaitable[object]] | None = ...,
        on_get_prompt: Callable[..., Awaitable[object]] | None = ...,
        on_completion: Callable[..., Awaitable[object]] | None = ...,
        on_set_logging_level: Callable[..., Awaitable[object]] | None = ...,
        on_ping: Callable[..., Awaitable[object]] = ...,
        on_roots_list_changed: Callable[..., Awaitable[None]] | None = ...,
        on_progress: Callable[..., Awaitable[None]] | None = ...,
    ) -> None: ...
    def create_initialization_options(
        self,
        notification_options: object | None = ...,
        experimental_capabilities: dict[str, dict[str, object]] | None = ...,
        extensions: dict[str, dict[str, object]] | None = ...,
    ) -> object: ...
    async def run(
        self,
        read_stream: object,
        write_stream: object,
        initialization_options: object,
        raise_exceptions: bool = ...,
    ) -> None: ...
