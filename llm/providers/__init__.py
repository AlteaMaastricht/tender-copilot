"""Provider adapters. These are the only modules allowed to import a provider SDK."""

from __future__ import annotations

from llm.providers.base import Provider


def default_providers() -> dict[str, Provider]:
    """The real adapters, keyed by the provider name used in `llm/prices.py`.

    Imported here, not at module level, so that the gateway core never loads an SDK. Each
    adapter reads its key from the environment on first use.
    """
    from llm.providers.anthropic import AnthropicProvider
    from llm.providers.mistral import MistralProvider

    return {"anthropic": AnthropicProvider(), "mistral": MistralProvider()}
