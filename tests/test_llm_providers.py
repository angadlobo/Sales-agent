"""Tests for the OpenAI-compatible provider path in sales_agent.llm.

A local stub server stands in for GitHub Models / OpenRouter / any custom
endpoint, so no network or API key is needed.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from pydantic import BaseModel

from sales_agent import config, llm


# ──────────────────────────────────────────────────────────────────────────
# Stub OpenAI-compatible server
# ──────────────────────────────────────────────────────────────────────────
class _Stub(BaseHTTPRequestHandler):
    # Filled per-test: list of (status, payload) responses, served in order.
    responses: list[tuple[int, dict]] = []
    requests: list[dict] = []

    def do_POST(self):  # noqa: N802 - http.server API
        assert self.path == "/chat/completions"
        length = int(self.headers["Content-Length"])
        _Stub.requests.append(json.loads(self.rfile.read(length)))
        status, payload = _Stub.responses[min(len(_Stub.requests) - 1, len(_Stub.responses) - 1)]
        if status == 0:  # sentinel: drop the connection without responding
            self.connection.close()
            return
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence
        pass


def _chat_reply(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


@pytest.fixture()
def stub_server(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _Stub.responses = []
    _Stub.requests = []
    monkeypatch.setenv("LLM_PROVIDER", "custom")
    monkeypatch.setenv("LLM_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.delenv("SALES_AGENT_MODEL", raising=False)
    yield _Stub
    server.shutdown()


# ──────────────────────────────────────────────────────────────────────────
# Provider config resolution
# ──────────────────────────────────────────────────────────────────────────
def test_github_provider_uses_github_token(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "github")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("SALES_AGENT_MODEL", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "github_pat_x")
    assert config.llm_base_url() == "https://models.github.ai/inference"
    assert config.llm_api_key() == "github_pat_x"
    assert config.default_model() == "openai/gpt-4.1"


def test_openrouter_provider_key_and_url(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-x")
    assert config.llm_base_url() == "https://openrouter.ai/api/v1"
    assert config.llm_api_key() == "sk-or-x"


def test_stale_claude_model_swapped_on_other_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "github")
    assert llm._resolve_model("claude-opus-4-8") == "openai/gpt-4.1"
    assert llm._resolve_model("claude-haiku-4-5") == "openai/gpt-4.1-mini"
    # prefixed ids pass through (e.g. OpenRouter's anthropic/claude-…)
    assert llm._resolve_model("anthropic/claude-opus-4.8") == "anthropic/claude-opus-4.8"


def test_anthropic_default_unchanged(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("SALES_AGENT_MODEL", raising=False)
    assert config.llm_provider() == "anthropic"
    assert llm._resolve_model(None) == "claude-opus-4-8"


# ──────────────────────────────────────────────────────────────────────────
# Chat / research / extract over the stub
# ──────────────────────────────────────────────────────────────────────────
def test_complete_roundtrip(stub_server):
    stub_server.responses = [(200, _chat_reply("Paris"))]
    out = llm.complete("Capital of France?", system="Be terse", model="test-model")
    assert out == "Paris"
    req = stub_server.requests[0]
    assert req["model"] == "test-model"
    assert req["messages"][0] == {"role": "system", "content": "Be terse"}
    assert req["messages"][1]["role"] == "user"


def test_max_completion_tokens_retry(stub_server):
    stub_server.responses = [
        (400, {"error": {"message": "Use 'max_completion_tokens' instead of 'max_tokens'"}}),
        (200, _chat_reply("ok")),
    ]
    assert llm.complete("hi", model="gpt-5") == "ok"
    assert "max_tokens" in stub_server.requests[0]
    retry = stub_server.requests[1]
    assert "max_tokens" not in retry
    assert "max_completion_tokens" in retry


def test_extract_parses_fenced_json(stub_server):
    class Person(BaseModel):
        name: str
        age: int

    stub_server.responses = [(200, _chat_reply('```json\n{"name": "Ada", "age": 36}\n```'))]
    person = llm.extract("Who?", Person, model="test-model")
    assert person == Person(name="Ada", age=36)
    # schema + JSON-only instruction included in the prompt
    prompt = stub_server.requests[0]["messages"][0]["content"]
    assert "JSON Schema" in prompt and '"age"' in prompt


def test_extract_repair_attempt(stub_server):
    class Person(BaseModel):
        name: str

    stub_server.responses = [
        (200, _chat_reply("sorry, here you go: name = Ada")),
        (200, _chat_reply('{"name": "Ada"}')),
    ]
    assert llm.extract("Who?", Person, model="test-model").name == "Ada"
    assert len(stub_server.requests) == 2


def test_research_plain_completion_without_web(stub_server):
    stub_server.responses = [(200, _chat_reply("notes"))]
    assert llm.research("find leads", model="test-model") == "notes"
    # custom provider gets no openrouter web plugin
    assert "plugins" not in stub_server.requests[0]


def test_research_openrouter_enables_web_plugin(stub_server, monkeypatch):
    base = config.llm_base_url()
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("LLM_BASE_URL", base)  # keep pointing at the stub
    stub_server.responses = [(200, _chat_reply("notes"))]
    assert llm.research("find leads", model="test-model") == "notes"
    assert stub_server.requests[0]["plugins"] == [{"id": "web"}]


def test_transient_connection_drop_is_retried(stub_server):
    # First request: server closes the connection mid-handshake ("peer closed
    # connection without sending complete message body"). Second succeeds.
    stub_server.responses = [(0, {}), (200, _chat_reply("ok"))]
    assert llm.complete("hi", model="test-model") == "ok"
    assert len(stub_server.requests) == 2


def test_missing_key_raises_clear_error(stub_server, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        llm.complete("hi", model="test-model")
