import pytest

from nemapi.core.errors import APIError
from nemapi.core.providers import guess_provider, resolve_provider


@pytest.mark.parametrize(
    "model,expected",
    [
        ("qwen-plus", "qwen"),
        ("qwen/qwen-chat", "qwen"),
        ("QWEN3-CODER-PLUS", "qwen"),
        ("deepseek-chat", "deepseek"),
        ("chat", "deepseek"),
        ("claude-sonnet", "claude"),
        ("claude-opus-4-1", "claude"),
        ("anthropic/claude-3.7-sonnet", "claude"),
        ("gemini-2.5-pro", "gemini"),
        ("gpt-4o-mini", "chatgpt"),
        ("o4-mini", "chatgpt"),
        ("openai/gpt-5", "chatgpt"),
        ("kimi-k2", "kimi"),
        ("glm-4.6", "zai"),
        ("zai/whatever", "zai"),
    ],
)
def test_guess_provider(model, expected):
    assert guess_provider(model) == expected


@pytest.mark.parametrize("model", ["", "zzz", "llama-3", "mistral-large"])
def test_unknown_models(model):
    assert guess_provider(model) is None


def test_resolve_provider_errors():
    with pytest.raises(APIError) as e:
        resolve_provider("", "")
    assert e.value.status_code == 400 and "required" in e.value.message
    with pytest.raises(APIError):
        resolve_provider("anything", "nope")
    assert resolve_provider("anything", "Claude") == "claude"
