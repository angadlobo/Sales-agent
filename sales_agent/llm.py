"""LLM wrapper: Anthropic Claude by default, or any OpenAI-compatible provider.

Centralizes model selection, the web-search agentic loop, and structured-output
parsing so the rest of the codebase never touches a raw client.

Set LLM_PROVIDER=github|openrouter|openai|custom to run on an OpenAI-compatible
/chat/completions endpoint (GitHub Models, OpenRouter, OpenAI, Groq, Ollama, …)
instead of Anthropic. See .env.example for the matching key/base-URL variables.
"""

from __future__ import annotations

import json
import logging
import time
from functools import lru_cache
from typing import Optional, Type, TypeVar

import anthropic
import httpx
from pydantic import BaseModel

from . import config

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Server-side web search tool. Runs on Anthropic's infrastructure — no key,
# no client-side execution. We only re-send on `pause_turn`.
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search"}

# Research calls can run for minutes when the model searches the web.
_HTTP_TIMEOUT = httpx.Timeout(300.0, connect=15.0)


@lru_cache(maxsize=1)
def get_client() -> anthropic.Anthropic:
    """Cached client. Reads ANTHROPIC_API_KEY from the environment."""
    return anthropic.Anthropic()


def _resolve_model(model: Optional[str]) -> str:
    """Fill in the provider-aware default and catch cross-provider model ids.

    A bare Claude id (a stale SALES_AGENT_MODEL or an import-time default left
    over from before a provider switch) can't be served by an OpenAI-compatible
    endpoint, so swap it for the active provider's preset. Prefixed ids like
    OpenRouter's "anthropic/claude-…" contain "/" and pass through untouched.
    """
    if model is None:
        model = config.default_model()
    if config.llm_provider() != "anthropic" and model.startswith("claude-"):
        main, fast = config.preset_models()
        swapped = fast if ("haiku" in model or "mini" in model) else main
        logger.debug(
            "Model %r is not served by provider %r; using %r instead",
            model, config.llm_provider(), swapped,
        )
        return swapped
    return model


def complete(
    prompt: str,
    *,
    system: Optional[str] = None,
    model: Optional[str] = None,
    max_tokens: int = 4000,
) -> str:
    """Single-shot text completion."""
    model = _resolve_model(model)
    if config.llm_provider() != "anthropic":
        return _oai_chat(_oai_messages(prompt, system), model=model, max_tokens=max_tokens)
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
    model: Optional[str] = None,
    max_tokens: int = 8000,
    max_continuations: int = 6,
) -> str:
    """Run a web-search-enabled turn and return the final text.

    Anthropic: server-side web search; we only resume on `pause_turn`.
    OpenRouter: the `web` plugin feeds live search results to the model.
    Other OpenAI-compatible providers (GitHub Models, OpenAI, custom) have no
    live web access here — the model answers from its own knowledge, so lead
    discovery is weaker and may be stale.
    """
    model = _resolve_model(model)
    provider = config.llm_provider()
    if provider != "anthropic":
        extra = {"plugins": [{"id": "web"}]} if provider == "openrouter" else None
        if extra is None:
            logger.debug("Provider %r has no live web search; using model knowledge only", provider)
        return _oai_chat(
            _oai_messages(prompt, system), model=model, max_tokens=max_tokens, extra_body=extra
        )

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


def extract(
    prompt: str,
    schema: Type[T],
    *,
    model: Optional[str] = None,
    max_tokens: int = 4000,
) -> T:
    """Ask the model for output that conforms exactly to a Pydantic schema."""
    model = _resolve_model(model)
    if config.llm_provider() != "anthropic":
        return _oai_extract(prompt, schema, model=model, max_tokens=max_tokens)
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


# ──────────────────────────────────────────────────────────────────────────
# OpenAI-compatible providers (GitHub Models, OpenRouter, OpenAI, custom)
# ──────────────────────────────────────────────────────────────────────────
def _oai_messages(prompt: str, system: Optional[str] = None) -> list[dict]:
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return messages


def _post_json(url: str, headers: dict, body: dict) -> httpx.Response:
    """POST with retries on transient transport failures.

    Free-tier endpoints (GitHub Models especially) sometimes drop the
    connection mid-response ("peer closed connection without sending complete
    message body"); the Anthropic SDK retries these itself, so match that here.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            return httpx.post(url, headers=headers, json=body, timeout=_HTTP_TIMEOUT)
        except httpx.TransportError as exc:
            last_exc = exc
            wait = 2 * (attempt + 1)
            logger.warning("Transient connection error (%s); retrying in %ss", exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"connection to the model provider failed after 3 attempts: {last_exc}") from last_exc


def _oai_chat(
    messages: list[dict],
    *,
    model: str,
    max_tokens: int,
    response_format: Optional[dict] = None,
    extra_body: Optional[dict] = None,
) -> str:
    provider = config.llm_provider()
    base_url = config.llm_base_url()
    api_key = config.llm_api_key()
    if not base_url:
        raise RuntimeError(f"LLM_BASE_URL is required for LLM_PROVIDER={provider!r}")
    if not api_key:
        raise RuntimeError(
            f"No API key for LLM_PROVIDER={provider!r}: set LLM_API_KEY "
            "(or GITHUB_TOKEN / OPENROUTER_API_KEY / OPENAI_API_KEY)"
        )

    headers = {"Authorization": f"Bearer {api_key}"}
    if provider == "openrouter":
        headers["X-Title"] = "Sales Agent"  # optional attribution

    body: dict = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if response_format:
        body["response_format"] = response_format
    if extra_body:
        body.update(extra_body)

    url = base_url.rstrip("/") + "/chat/completions"
    resp = _post_json(url, headers, body)
    # Newer OpenAI models reject max_tokens in favour of max_completion_tokens,
    # and some providers reject response_format. Drop whatever the server
    # complained about and retry.
    for _ in range(2):
        if resp.status_code != 400:
            break
        error_text = resp.text
        changed = False
        if "max_completion_tokens" in error_text and "max_tokens" in body:
            body["max_completion_tokens"] = body.pop("max_tokens")
            changed = True
        if "response_format" in error_text and "response_format" in body:
            body.pop("response_format")
            changed = True
        if not changed:
            break
        resp = _post_json(url, headers, body)
    # Free tiers (GitHub Models especially) have tight per-minute limits and
    # the pipeline fires several calls in quick succession — wait and retry.
    for _ in range(3):
        if resp.status_code != 429:
            break
        try:
            wait = min(int(resp.headers.get("retry-after") or 15), 60)
        except ValueError:
            wait = 15
        logger.warning("%s rate-limited (429); retrying in %ss", provider, wait)
        time.sleep(wait)
        resp = _post_json(url, headers, body)
    if resp.status_code >= 400:
        text = resp.text[:500]
        hint = ""
        if "unavailable_model" in text:
            hint = (" — this model isn't available on your plan. Pick the provider default "
                    "in Settings → AI model (e.g. openai/gpt-4.1 on GitHub Models).")
        elif resp.status_code == 429:
            hint = (" — provider rate limit. Wait a minute and retry, lower max leads, "
                    "or pick a higher-limit model (e.g. openai/gpt-4.1-mini).")
        raise RuntimeError(f"{provider} API error {resp.status_code}: {text}{hint}")
    return resp.json()["choices"][0]["message"]["content"] or ""


def _oai_extract(prompt: str, schema: Type[T], *, model: str, max_tokens: int) -> T:
    schema_json = json.dumps(schema.model_json_schema(), indent=2)
    full_prompt = (
        f"{prompt}\n\n"
        "Respond with a single JSON object that conforms to this JSON Schema. "
        "Output only the JSON — no prose, no markdown fences.\n\n"
        f"{schema_json}"
    )
    text = _oai_chat(
        _oai_messages(full_prompt),
        model=model,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
    )
    try:
        return schema.model_validate_json(_json_payload(text))
    except Exception:  # noqa: BLE001 - one repair attempt before giving up
        repair = (
            "The following output was supposed to be a JSON object matching this "
            "schema but is invalid or incomplete. Return the corrected JSON object "
            f"only.\n\nSchema:\n{schema_json}\n\nOutput to fix:\n{text[:4000]}"
        )
        fixed = _oai_chat(
            _oai_messages(repair),
            model=model,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
        )
        try:
            return schema.model_validate_json(_json_payload(fixed))
        except Exception as exc:
            raise RuntimeError(
                f"model did not return parseable {schema.__name__}: {text[:300]}"
            ) from exc


def _json_payload(text: str) -> str:
    """Trim markdown fences and surrounding prose down to the JSON object."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]
    return text
