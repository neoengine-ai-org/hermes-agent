from gateway import run
from gateway.runtime.delivery_redaction import redact_gateway_user_facing_secrets


def test_gateway_run_redaction_facade_preserves_public_private_import_surface():
    raw = "token sk-testSecretValue123456 and Bearer abcdefghijklmnopqrstuvwxyz"
    assert run._redact_gateway_user_facing_secrets(raw) == redact_gateway_user_facing_secrets(raw)
    assert "[REDACTED]" in run._redact_gateway_user_facing_secrets(raw)
    assert "sk-testSecretValue123456" not in run._redact_gateway_user_facing_secrets(raw)


def test_gateway_provider_error_reply_still_uses_redacted_text():
    raw = "Provider authentication failed: sk-testSecretValue123456"
    sanitized = run._sanitize_gateway_final_response('telegram', raw)
    assert "Provider authentication failed" in sanitized
    assert "sk-testSecretValue123456" not in sanitized
