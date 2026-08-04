import pytest

from gjp_cli.model_runtime import (
    LLMSettings,
    build_chat_model,
    supported_model_providers,
)
from gjp_common.errors import DomainError


EXPECTED_PROVIDERS = {
    "anthropic",
    "dashscope",
    "deepseek",
    "gemini",
    "moonshot",
    "ollama",
    "openai",
    "xai",
}


def settings_for(provider: str, **overrides):
    values = {
        "provider": provider,
        "model_name": "test-model",
        "api_key": "test-key",
        "base_url": None,
        "stream": True,
        "parameters": {},
        "timeout_seconds": 30,
        "max_retries": 3,
        "context_size": None,
    }
    values.update(overrides)
    return LLMSettings(**values)


def test_all_agentscope_chat_providers_build_with_native_streaming():
    assert set(supported_model_providers()) == EXPECTED_PROVIDERS

    for provider in sorted(EXPECTED_PROVIDERS):
        model = build_chat_model(settings_for(provider))
        assert model.stream is True
        assert model.model == "test-model"


def test_stream_false_is_forwarded_to_agentscope_model():
    model = build_chat_model(settings_for("openai", stream=False))

    assert model.stream is False


def test_provider_parameters_are_validated_by_agentscope_schema():
    with pytest.raises(DomainError, match="不支持模型参数"):
        build_chat_model(
            settings_for("anthropic", parameters={"temperature": 0.1}),
        )
