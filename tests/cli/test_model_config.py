import pytest

from gjp_cli.model_runtime import LLMSettings
from gjp_common.errors import DomainError


ENV_NAMES = (
    "LLM_TEXT_PROVIDER",
    "LLM_TEXT_MODEL_NAME",
    "LLM_TEXT_API_KEY",
    "LLM_TEXT_BASE_URL",
    "LLM_TEXT_STREAM",
    "LLM_TEXT_PARAMETERS",
    "LLM_TEXT_TIMEOUT_SECONDS",
    "LLM_TEXT_MAX_RETRIES",
    "LLM_TEXT_CONTEXT_SIZE",
)


def test_settings_load_agentscope_model_configuration(tmp_path, monkeypatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "LLM_TEXT_PROVIDER=anthropic",
                "LLM_TEXT_MODEL_NAME=claude-sonnet-4-6",
                "LLM_TEXT_API_KEY=test-key",
                "LLM_TEXT_BASE_URL=https://anthropic.example.test",
                "LLM_TEXT_STREAM=true",
                'LLM_TEXT_PARAMETERS={"max_tokens":4096}',
                "LLM_TEXT_TIMEOUT_SECONDS=45",
                "LLM_TEXT_MAX_RETRIES=5",
                "LLM_TEXT_CONTEXT_SIZE=200000",
            ],
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GJP_ENV_FILE", str(env_file))

    settings = LLMSettings.from_env()

    assert settings.provider == "anthropic"
    assert settings.credential_type == "anthropic_credential"
    assert settings.model_name == "claude-sonnet-4-6"
    assert settings.api_key == "test-key"
    assert settings.stream is True
    assert settings.parameters == {"max_tokens": 4096}
    assert settings.timeout_seconds == 45
    assert settings.max_retries == 5
    assert settings.context_size == 200000


def test_process_environment_overrides_dotenv(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_TEXT_API_KEY=from-file", encoding="utf-8")
    monkeypatch.setenv("GJP_ENV_FILE", str(env_file))
    monkeypatch.setenv("LLM_TEXT_API_KEY", "from-process")

    assert LLMSettings.from_env().api_key == "from-process"


def test_invalid_stream_value_is_rejected(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "LLM_TEXT_API_KEY=test\nLLM_TEXT_STREAM=sometimes",
        encoding="utf-8",
    )
    monkeypatch.setenv("GJP_ENV_FILE", str(env_file))

    with pytest.raises(DomainError, match="LLM_TEXT_STREAM"):
        LLMSettings.from_env()


def test_vision_settings_load_from_llm_vision_env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "LLM_VISION_PROVIDER=openai",
                "LLM_VISION_MODEL_NAME=qwen3-vl-plus",
                "LLM_VISION_API_KEY=vision-key",
                "LLM_VISION_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1",
                "LLM_VISION_PARAMETERS={}",
                "LLM_VISION_TIMEOUT_SECONDS=120",
            ],
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GJP_ENV_FILE", str(env_file))

    settings = LLMSettings.vision_from_env()

    assert settings.env_prefix == "LLM_VISION"
    assert settings.provider == "openai"
    assert settings.model_name == "qwen3-vl-plus"
    assert settings.api_key == "vision-key"
    assert settings.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert settings.stream is False
    assert settings.timeout_seconds == 120


def test_vision_settings_require_model_name(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_VISION_API_KEY=vision-key", encoding="utf-8")
    monkeypatch.setenv("GJP_ENV_FILE", str(env_file))

    with pytest.raises(DomainError, match="LLM_VISION_MODEL_NAME"):
        LLMSettings.vision_from_env()
