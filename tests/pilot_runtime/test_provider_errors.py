from offerpilot.config import Config
from offerpilot.context_projector.contracts import ProjectionError
from offerpilot.pilot_runtime.composition import _provider_error_message


def test_budget_projection_error_points_to_explicit_ai_budget_settings() -> None:
    message = _provider_error_message(
        ProjectionError("mandatory_surface_over_budget"),
        Config(),
    )

    assert message == (
        "模型上下文配置不足：当前请求的必要内容超出已配置窗口。"
        "请在 AI 设置中填写正确的上下文窗口和单次最大输出。"
    )


def test_ordinary_provider_error_keeps_existing_safe_message() -> None:
    assert _provider_error_message(RuntimeError("provider unavailable"), Config()) == (
        "AI 连接失败：provider unavailable。请检查 AI 设置或稍后重试。"
    )
