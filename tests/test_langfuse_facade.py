"""OFW exposes the supported official Langfuse v4 surface unchanged."""

import langfuse

import ofw


def test_supported_langfuse_exports_preserve_identity() -> None:
    assert ofw.Langfuse is langfuse.Langfuse
    assert ofw.LangfuseOtelSpanAttributes is langfuse.LangfuseOtelSpanAttributes
    assert ofw.LangfuseSpan is langfuse.LangfuseSpan  # type: ignore[misc]
    assert ofw.get_client is langfuse.get_client
    assert ofw.is_default_export_span is langfuse.is_default_export_span
    assert ofw.observe is langfuse.observe  # type: ignore[misc]
    assert ofw.propagate_attributes is langfuse.propagate_attributes  # type: ignore[misc]
