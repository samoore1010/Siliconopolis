"""Guards against sending kwargs the installed Anthropic SDK rejects (e.g. `temperature`)."""

import contextlib
import inspect

import anthropic
import pytest


class FakeResp:
    content = []
    stop_reason = "end_turn"

    class usage:
        input_tokens = 1
        output_tokens = 1


def _patch_stream(monkeypatch, llm, seen, resp=FakeResp):
    class FakeStream:
        def get_final_message(self):
            return resp

    @contextlib.contextmanager
    def fake_stream(**kwargs):
        seen.update(kwargs)
        yield FakeStream()

    monkeypatch.setattr(llm._client.messages, "stream", fake_stream)


def test_messages_stream_kwargs_supported(monkeypatch):
    from pipeline.llm import AnthropicLLM

    allowed = set(inspect.signature(anthropic.resources.messages.Messages.stream).parameters)
    seen = {}
    llm = AnthropicLLM("test-key")
    _patch_stream(monkeypatch, llm, seen)
    llm.complete(model="claude-sonnet-5-5", system="s", user="u", task="t")
    unsupported = set(seen) - allowed
    assert not unsupported, f"kwargs not accepted by anthropic {anthropic.__version__}: {unsupported}"


def test_truncated_reply_gives_clear_error(monkeypatch):
    from pipeline.llm import AnthropicLLM, LLMError

    class Block:
        type = "text"
        text = '{"briefs": [{"concept": "cut off'

    class Truncated(FakeResp):
        content = [Block()]
        stop_reason = "max_tokens"

    llm = AnthropicLLM("test-key")
    _patch_stream(monkeypatch, llm, {}, Truncated)
    result = llm.complete(model="claude-sonnet-5-5", system="s", user="u", task="t")
    assert result.truncated
    with pytest.raises(LLMError, match="max_tokens"):
        result.json()


def test_etsy_api_key_header_includes_shared_secret():
    from pipeline.adapters.etsy.live import LiveEtsyClient

    assert LiveEtsyClient(api_key="key", shop_id="1", dry_run=True, shared_secret="sec").api_key_header == "key:sec"
    assert LiveEtsyClient(api_key="key", shop_id="1", dry_run=True).api_key_header == "key"


def test_extract_json_tolerates_what_models_actually_send():
    from pipeline.llm import extract_json

    raw_newlines = '{"pass": false, "reasons": ["line one\nline two"], "scores": {"quality": 5,}}'
    assert extract_json(raw_newlines)["reasons"] == ["line one\nline two"]
    prose = 'Here is my review:\n```json\n{"pass": true, "scores": {"quality": 8}}\n```\nLet me know!'
    assert extract_json(prose)["pass"] is True
    wrapped = 'Sure. {"a": [1, 2, 3,], "b": "x"} Done.'
    assert extract_json(wrapped) == {"a": [1, 2, 3], "b": "x"}


def test_ask_json_re_asks_once_when_the_reply_is_not_json(session, monkeypatch):
    from pipeline.agents.qa import QAAgent
    from pipeline.llm import LLMError, MockLLM, get_llm

    agent = QAAgent()
    replies = ["Scores: {quality: 5, 'legibility': not json", '{"pass": true, "scores": {"quality": 8}}']
    monkeypatch.setattr(MockLLM, "_canned", staticmethod(lambda task, user: replies.pop(0)))
    data, result = agent.ask_json(session, task="qa_vision", model="m", user="review this")
    assert data["pass"] is True and not replies
    calls = [c for c in get_llm().calls if c["task"] == "qa_vision"]
    assert len(calls) == 2 and "could not be parsed" in calls[1]["user"]
    replies[:] = ["still bad", "still bad"]
    import pytest

    with pytest.raises(LLMError):
        agent.ask_json(session, task="qa_vision", model="m", user="review this")
