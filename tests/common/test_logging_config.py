import logging

from gjp_common.logging_config import configure_logging


def _logging_env(tmp_path, enabled: str, level: str = "INFO"):
    path = tmp_path / ".env"
    path.write_text(
        "GJP_LOG_ENABLED=%s\nGJP_LOG_LEVEL=%s\nGJP_LOG_CONTEXT=true\n"
        % (enabled, level),
        encoding="utf-8",
    )
    return path


def test_terminal_logging_can_be_disabled(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GJP_LOG_ENABLED", raising=False)
    monkeypatch.delenv("GJP_LOG_LEVEL", raising=False)
    monkeypatch.setenv("GJP_ENV_FILE", str(_logging_env(tmp_path, "false")))

    assert configure_logging() is False
    logging.getLogger("gjp_common.test").info("不应输出")

    assert capsys.readouterr().err == ""


def test_terminal_logging_writes_execution_stage_to_stderr(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GJP_LOG_ENABLED", raising=False)
    monkeypatch.delenv("GJP_LOG_LEVEL", raising=False)
    monkeypatch.setenv("GJP_ENV_FILE", str(_logging_env(tmp_path, "true", "INFO")))

    assert configure_logging() is True
    logging.getLogger("gjp_common.test").info("执行阶段=compile")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "终端执行日志已开启" in captured.err
    assert "model_context=True" in captured.err
    assert "执行阶段=compile" in captured.err
