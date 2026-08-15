from typing import TypeVar

F = TypeVar("F")

class TextContent:
    type: str
    text: str
    def __init__(self, *, type: str, text: str) -> None: ...

class Tool:
    name: str
    description: str
    input_schema: dict[str, object]
    def __init__(
        self,
        *,
        name: str,
        description: str,
        input_schema: dict[str, object],
    ) -> None: ...

class ListToolsResult:
    tools: list[Tool]
    def __init__(self, *, tools: list[Tool]) -> None: ...

class CallToolResult:
    content: list[TextContent]
    is_error: bool
    def __init__(
        self,
        *,
        content: list[TextContent],
        is_error: bool = ...,
    ) -> None: ...

class CallToolRequestParams:
    name: str
    arguments: dict[str, object] | None

class PaginatedRequestParams:
    pass

class EmptyResult:
    pass
