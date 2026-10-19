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
