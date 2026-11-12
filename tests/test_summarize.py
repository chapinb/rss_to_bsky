"""Tests for the OpenAI-compatible LLM client, against a mocked API."""

import json
import logging

import httpx
import pytest

from rss_post import summarize

KEY = "sk-test-secret"


def answer(content="• One\n• Two", finish_reason="stop", **message):
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "m",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, **message},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


class API:
    """A mocked provider that answers with ``responses`` in turn, repeating
    the last; an exception is raised instead of answering."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        response = (
            self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        )
        if isinstance(response, Exception):
            raise response
        return response

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self))


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", KEY)


def ok(content="• One\n• Two", **kwargs):
    return httpx.Response(200, json=answer(content, **kwargs))


def test_without_a_key_nothing_is_requested():
    api = API(ok())
    assert not summarize.configured()
    assert not summarize.enabled()
    assert summarize.summarize("text", "instructions", client=api.client()) is None
    assert api.requests == []


def test_request_follows_the_chat_completions_spec(key):
    api = API(ok())
    assert summarize.summarize("agenda", "be brief", client=api.client()) == (
        "• One\n• Two"
    )

    [request] = api.requests
    assert request.method == "POST"
    assert str(request.url) == (
        "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
    )
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert request.headers["Content-Type"] == "application/json"
    assert json.loads(request.content) == {
        "model": summarize.DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "agenda"},
        ],
        "temperature": summarize.TEMPERATURE,
        "max_tokens": summarize.MAX_TOKENS,
    }


def test_default_model_is_pinned_to_a_stable_flash_model():
    assert summarize.DEFAULT_MODEL == "gemini-3.5-flash-lite"
    assert "latest" not in summarize.DEFAULT_MODEL
    assert "preview" not in summarize.DEFAULT_MODEL


@pytest.mark.parametrize(
    "base_url", ["https://api.example.com/v1", "https://api.example.com/v1/"]
)
def test_base_url_and_model_are_configurable(key, monkeypatch, base_url):
    monkeypatch.setenv("LLM_BASE_URL", base_url)
    monkeypatch.setenv("LLM_MODEL", "some-model")
    api = API(ok())
    summarize.summarize("agenda", "be brief", client=api.client())

    [request] = api.requests
    assert str(request.url) == "https://api.example.com/v1/chat/completions"
    assert json.loads(request.content)["model"] == "some-model"


def test_input_is_capped(key):
    api = API(ok())
    summarize.summarize("x" * 50_000, "be brief", client=api.client())

    body = json.loads(api.requests[0].content)
    assert len(body["messages"][1]["content"]) == summarize.MAX_INPUT_CHARS


def test_answer_is_stripped(key):
    api = API(ok("\n  • One\n• Two  \n"))
    assert summarize.summarize("t", "i", client=api.client()) == "• One\n• Two"


@pytest.mark.parametrize(
    "response",
    [
        ok(""),
        ok("   "),
        ok(None),
        ok("I can't help with that.", refusal="I can't help with that."),
        ok("• One\n• Tw", finish_reason="length"),
        ok("", finish_reason="content_filter"),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"error": "unexpected"}),
        httpx.Response(200, text="<html>not json</html>"),
    ],
)
def test_unusable_answers_give_none(key, response):
    assert summarize.summarize("t", "i", client=API(response).client()) is None


def test_overlong_answer_gives_none(key):
    api = API(ok("x" * 101))
    assert summarize.summarize("t", "i", max_chars=100, client=api.client()) is None
    assert summarize.summarize("t", "i", max_chars=101, client=api.client()) == (
        "x" * 101
    )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401),
        httpx.Response(403),
        httpx.Response(404),
        httpx.Response(400, json={"error": {"message": "model not found"}}),
        httpx.Response(500),
        httpx.ConnectError("unreachable"),
        httpx.ReadTimeout("timed out"),
    ],
)
def test_first_failure_turns_the_llm_off_for_the_run(key, response):
    api = API(response, ok())
    assert summarize.summarize("t", "i", client=api.client()) is None
    assert not summarize.enabled()
    # No further requests this run, even though the API would now answer.
    assert summarize.summarize("t", "i", client=api.client()) is None
    assert len(api.requests) == 1

    summarize.reset()  # The next run tries again.
    assert summarize.summarize("t", "i", client=api.client()) == "• One\n• Two"


@pytest.mark.parametrize(
    "response",
    [httpx.Response(500), httpx.Response(429), httpx.ReadTimeout("timed out")],
)
def test_transient_failure_after_a_success_skips_only_that_summary(key, response):
    api = API(ok(), response, ok())
    assert summarize.summarize("t", "i", client=api.client()) is not None
    assert summarize.summarize("t", "i", client=api.client()) is None
    assert summarize.enabled()
    assert summarize.summarize("t", "i", client=api.client()) is not None
    assert len(api.requests) == 3


def test_rejected_key_after_a_success_turns_the_llm_off(key):
    api = API(ok(), httpx.Response(401), ok())
    summarize.summarize("t", "i", client=api.client())
    assert summarize.summarize("t", "i", client=api.client()) is None
    assert summarize.summarize("t", "i", client=api.client()) is None
    assert len(api.requests) == 2


def test_llm_time_per_run_is_capped(key, monkeypatch):
    api = API(ok())
    summarize.summarize("t", "i", client=api.client())
    monkeypatch.setattr(summarize._state, "seconds", summarize.MAX_SECONDS_PER_RUN)
    assert summarize.summarize("t", "i", client=api.client()) is None
    assert not summarize.enabled()
    assert len(api.requests) == 1


def test_timeouts_are_short():
    assert summarize.TIMEOUT.read <= 20
    assert summarize.TIMEOUT.connect <= 20
    with summarize._new_client() as client:
        assert client.timeout == summarize.TIMEOUT


def test_the_key_is_never_logged(key, caplog):
    caplog.set_level(logging.DEBUG)
    for response in (
        httpx.Response(401, text=f"bad key {KEY}"),
        httpx.ConnectError(f"failed for {KEY}"),
        ok("x" * 500),
    ):
        summarize.reset()
        summarize.summarize("t", "i", max_chars=10, client=API(response).client())
    assert caplog.records
    assert KEY not in caplog.text
