"""Thin wrapper around the Anthropic SDK.

Centralizes model selection, the web-search agentic loop, and structured-output
parsing so the rest of the codebase never touches the raw client.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Optional, Type, TypeVar

import anthropic
from pydantic import BaseModel

from .config import DEFAULT_MODEL

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Server-side web search tool. Runs on Anthropic's infrastructure — no key,
# no client-side execution. We only re-send on `pause_turn`.
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search"}


@lru_cache(maxsize=1)
def get_client() -> anthropic.Anthropic:
    """Cached client. Reads ANTHROPIC_API_KEY from the environment."""
    return anthropic.Anthropic()


def complete(
    prompt: str,
    *,
    system: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 4000,
) -> str:
    """Single-shot text completion."""
    client = get_client()
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system or anthropic.NOT_GIVEN,
        messages=[{"role": "user", "content": prompt}],
    )
    return _text(resp)


def research(
    prompt: str,
    *,
    system: Optional[str] = None,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 8000,
    max_continuations: int = 6,
) -> str:
    """Run a web-search-enabled turn and return the final text.

    Web search is server-side, so there are no client tools to execute. We only
    have to resume when the server pauses its own tool loop (`pause_turn`).
    """
    client = get_client()
    messages = [{"role": "user", "content": prompt}]
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system or anthropic.NOT_GIVEN,
        tools=[WEB_SEARCH_TOOL],
        messages=messages,
    )

    continuations = 0
    while resp.stop_reason == "pause_turn" and continuations < max_continuations:
        messages.append({"role": "assistant", "content": resp.content})
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system or anthropic.NOT_GIVEN,
            tools=[WEB_SEARCH_TOOL],
            messages=messages,
        )
        continuations += 1

    return _text(resp)


def extract(prompt: str, schema: Type[T], *, model: str = DEFAULT_MODEL, max_tokens: int = 4000) -> T:
    """Ask Claude for output that conforms exactly to a Pydantic schema."""
    client = get_client()
    resp = client.messages.parse(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        output_format=schema,
    )
    parsed = resp.parsed_output
    if parsed is None:
        raise RuntimeError(f"Claude did not return parseable {schema.__name__}: {_text(resp)[:300]}")
    return parsed


def _text(resp) -> str:
    """Concatenate all text blocks in a response."""
    return "".join(block.text for block in resp.content if getattr(block, "type", None) == "text")
