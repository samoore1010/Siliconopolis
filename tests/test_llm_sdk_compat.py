"""Guards against sending kwargs the installed Anthropic SDK rejects (e.g. `temperature`)."""

import inspect

import anthropic


def test_messages_create_kwargs_supported(monkeypatch):
    from pipeline.llm import AnthropicLLM

    allowed = set(inspect.signature(anthropic.resources.messages.Messages.create).parameters)
    seen = {}

    class FakeResp:
        content = []

        class usage:
            input_tokens = 1
            output_tokens = 1

    def fake_create(**kwargs):
        seen.update(kwargs)
        return FakeResp()

    llm = AnthropicLLM("test-key")
    monkeypatch.setattr(llm._client.messages, "create", fake_create)
    llm.complete(model="claude-sonnet-5-5", system="s", user="u", task="t")
    unsupported = set(seen) - allowed
    assert not unsupported, f"kwargs not accepted by anthropic {anthropic.__version__}: {unsupported}"
