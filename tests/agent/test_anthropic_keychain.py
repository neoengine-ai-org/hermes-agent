"""Tests for Bug #12905 fixes in agent/anthropic_adapter.py — macOS Keychain support."""

import json
from unittest.mock import patch, MagicMock

import pytest

from agent.anthropic_adapter import (
    _read_claude_code_credentials_from_keychain,
    read_claude_code_credentials,
    _refresh_oauth_token,
)


class TestReadClaudeCodeCredentialsFromKeychain:
    """Bug 4: macOS Keychain support for Claude Code >=2.1.114."""

    def test_returns_none_on_linux(self):
        """Keychain reading is Darwin-only; must return None on other platforms."""
        with patch("agent.anthropic_adapter.platform.system", return_value="Linux"):
            assert _read_claude_code_credentials_from_keychain() is None

    def test_returns_none_on_windows(self):
        with patch("agent.anthropic_adapter.platform.system", return_value="Windows"):
            assert _read_claude_code_credentials_from_keychain() is None

    def test_returns_none_when_security_command_not_found(self):
        """OSError from missing security binary must be handled gracefully."""
        with patch("agent.anthropic_adapter.platform.system", return_value="Darwin"), \
             patch("agent.anthropic_adapter.subprocess.run",
                   side_effect=OSError("security not found")):
            assert _read_claude_code_credentials_from_keychain() is None

    def test_returns_none_on_nonzero_exit_code(self):
        """security returns non-zero when the Keychain entry doesn't exist."""
        with patch("agent.anthropic_adapter.platform.system", return_value="Darwin"), \
             patch("agent.anthropic_adapter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            assert _read_claude_code_credentials_from_keychain() is None

    def test_returns_none_for_empty_stdout(self):
        with patch("agent.anthropic_adapter.platform.system", return_value="Darwin"), \
             patch("agent.anthropic_adapter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            assert _read_claude_code_credentials_from_keychain() is None

    def test_returns_none_for_non_json_payload(self):
        with patch("agent.anthropic_adapter.platform.system", return_value="Darwin"), \
             patch("agent.anthropic_adapter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="not valid json", stderr="")
            assert _read_claude_code_credentials_from_keychain() is None

    def test_returns_none_when_password_field_is_missing_claude_ai_oauth(self):
        with patch("agent.anthropic_adapter.platform.system", return_value="Darwin"), \
             patch("agent.anthropic_adapter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({"someOtherService": {"accessToken": "tok"}}),
                stderr="",
            )
            assert _read_claude_code_credentials_from_keychain() is None

    def test_returns_none_when_access_token_is_empty(self):
        with patch("agent.anthropic_adapter.platform.system", return_value="Darwin"), \
             patch("agent.anthropic_adapter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({"claudeAiOauth": {"accessToken": "", "refreshToken": "x"}}),
                stderr="",
            )
            assert _read_claude_code_credentials_from_keychain() is None

    def test_parses_valid_keychain_entry(self):
        with patch("agent.anthropic_adapter.platform.system", return_value="Darwin"), \
             patch("agent.anthropic_adapter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({
                    "claudeAiOauth": {
                        "accessToken": "kc-access-token-abc",
                        "refreshToken": "kc-refresh-token-xyz",
                        "expiresAt": 9999999999999,
                    }
                }),
                stderr="",
            )
            creds = _read_claude_code_credentials_from_keychain()
            assert creds is not None
            assert creds["accessToken"] == "kc-access-token-abc"
            assert creds["refreshToken"] == "kc-refresh-token-xyz"
            assert creds["expiresAt"] == 9999999999999
            assert creds["source"] == "macos_keychain"


class TestReadClaudeCodeCredentialsPriority:
    """Bug 4: Keychain must be checked before the JSON file."""

    def test_keychain_takes_priority_over_json_file(self, tmp_path, monkeypatch):
        """When both Keychain and JSON file have credentials, Keychain wins."""
        # Set up JSON file with "older" token
        json_cred_file = tmp_path / ".claude" / ".credentials.json"
        json_cred_file.parent.mkdir(parents=True)
        json_cred_file.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "json-token",
                "refreshToken": "json-refresh",
                "expiresAt": 9999999999999,
            }
        }))
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)

        # Mock Keychain to return a "newer" token
        with patch("agent.anthropic_adapter.platform.system", return_value="Darwin"), \
             patch("agent.anthropic_adapter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps({
                    "claudeAiOauth": {
                        "accessToken": "keychain-token",
                        "refreshToken": "keychain-refresh",
                        "expiresAt": 9999999999999,
                    }
                }),
                stderr="",
            )
            creds = read_claude_code_credentials()

        # Keychain token should be returned, not JSON file token
        assert creds is not None
        assert creds["accessToken"] == "keychain-token"
        assert creds["source"] == "macos_keychain"

    def test_falls_back_to_json_when_keychain_returns_none(self, tmp_path, monkeypatch):
        """When Keychain has no entry, JSON file is used as fallback."""
        json_cred_file = tmp_path / ".claude" / ".credentials.json"
        json_cred_file.parent.mkdir(parents=True)
        json_cred_file.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "json-fallback-token",
                "refreshToken": "json-refresh",
                "expiresAt": 9999999999999,
            }
        }))
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)

        with patch("agent.anthropic_adapter.platform.system", return_value="Darwin"), \
             patch("agent.anthropic_adapter.subprocess.run") as mock_run:
            # Simulate Keychain entry not found
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            creds = read_claude_code_credentials()

        assert creds is not None
        assert creds["accessToken"] == "json-fallback-token"
        assert creds["source"] == "claude_code_credentials_file"

    def test_returns_none_when_neither_keychain_nor_json_has_creds(self, tmp_path, monkeypatch):
        """No credentials anywhere — must return None cleanly."""
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)

        with patch("agent.anthropic_adapter.platform.system", return_value="Darwin"), \
             patch("agent.anthropic_adapter.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            creds = read_claude_code_credentials()

        assert creds is None


class TestReadClaudeCodeCredentialsDesync:
    """Reconciliation when Keychain and JSON file disagree.

    Observed in the wild on Claude Code 2.1.x: a refresh updates one source
    (commonly the JSON file) but leaves the other holding an expired token.
    The reader must not blindly return whichever source it consulted first;
    it must prefer the non-expired credential.
    """

    # Far-future ms-epoch — comfortably valid under is_claude_code_token_valid.
    _FRESH = 9_999_999_999_999
    # Past ms-epoch — comfortably expired (with the 60s buffer).
    _EXPIRED = 1

    def _setup(self, tmp_path, monkeypatch, *, file_expires_at, file_token="json-token"):
        json_cred_file = tmp_path / ".claude" / ".credentials.json"
        json_cred_file.parent.mkdir(parents=True)
        json_cred_file.write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": file_token,
                "refreshToken": "json-refresh",
                "expiresAt": file_expires_at,
            }
        }))
        monkeypatch.setattr("agent.anthropic_adapter.Path.home", lambda: tmp_path)

    def _keychain_payload(self, *, access_token, expires_at, refresh_token="kc-refresh"):
        return MagicMock(
            returncode=0,
            stdout=json.dumps({
                "claudeAiOauth": {
                    "accessToken": access_token,
                    "refreshToken": refresh_token,
                    "expiresAt": expires_at,
                }
            }),
            stderr="",
        )

    def test_keychain_expired_file_fresh_returns_file(self, tmp_path, monkeypatch):
        """Regression: when the Keychain holds an expired token but the JSON
        file has a valid one, callers must receive the valid file token rather
        than None. (Pre-fix behavior returned the expired Keychain token, and
        downstream validity checks then yielded None — surfacing the misleading
        ``No Anthropic credentials found`` error.)
        """
        self._setup(tmp_path, monkeypatch, file_expires_at=self._FRESH, file_token="fresh-file-token")
        with patch("agent.anthropic_adapter.platform.system", return_value="Darwin"), \
             patch("agent.anthropic_adapter.subprocess.run") as mock_run:
            mock_run.return_value = self._keychain_payload(
                access_token="stale-keychain-token", expires_at=self._EXPIRED,
            )
            creds = read_claude_code_credentials()

        assert creds is not None
        assert creds["accessToken"] == "fresh-file-token"
        assert creds["source"] == "claude_code_credentials_file"

    def test_keychain_fresh_file_expired_returns_keychain(self, tmp_path, monkeypatch):
        """Mirror case: file is the stale source; Keychain wins on validity."""
        self._setup(tmp_path, monkeypatch, file_expires_at=self._EXPIRED, file_token="stale-file-token")
        with patch("agent.anthropic_adapter.platform.system", return_value="Darwin"), \
             patch("agent.anthropic_adapter.subprocess.run") as mock_run:
            mock_run.return_value = self._keychain_payload(
                access_token="fresh-keychain-token", expires_at=self._FRESH,
            )
            creds = read_claude_code_credentials()

        assert creds is not None
        assert creds["accessToken"] == "fresh-keychain-token"
        assert creds["source"] == "macos_keychain"

    def test_both_valid_prefers_later_expiry_when_file_is_fresher(self, tmp_path, monkeypatch):
        """When both are valid, the one with the later ``expiresAt`` wins so
        that any subsequent refresh uses the freshest ``refresh_token``.
        """
        self._setup(tmp_path, monkeypatch, file_expires_at=self._FRESH, file_token="newer-file-token")
        with patch("agent.anthropic_adapter.platform.system", return_value="Darwin"), \
             patch("agent.anthropic_adapter.subprocess.run") as mock_run:
            mock_run.return_value = self._keychain_payload(
                access_token="older-keychain-token", expires_at=self._FRESH - 1_000_000,
            )
            creds = read_claude_code_credentials()

        assert creds is not None
        assert creds["accessToken"] == "newer-file-token"

    def test_both_expired_prefers_later_expiry(self, tmp_path, monkeypatch):
        """When both are expired, return the one with the later ``expiresAt``;
        its ``refresh_token`` is the most recently issued and most likely to
        succeed at the OAuth refresh endpoint.
        """
        self._setup(tmp_path, monkeypatch, file_expires_at=self._EXPIRED + 5, file_token="newer-expired-file")
        with patch("agent.anthropic_adapter.platform.system", return_value="Darwin"), \
             patch("agent.anthropic_adapter.subprocess.run") as mock_run:
            mock_run.return_value = self._keychain_payload(
                access_token="older-expired-keychain", expires_at=self._EXPIRED,
            )
            creds = read_claude_code_credentials()

        assert creds is not None
        assert creds["accessToken"] == "newer-expired-file"


class TestRefreshOAuthTokenAdoptsFreshCredential:
    """``_refresh_oauth_token`` should adopt a credential Claude Code has
    already refreshed rather than POSTing a (possibly already-rotated)
    single-use refresh token and racing Claude Code into ``invalid_grant``.
    """

    _FRESH = 9_999_999_999_999

    def test_adopts_already_refreshed_token_without_posting(self, monkeypatch):
        """When a live source already holds a valid token, return it and skip
        the network refresh entirely.
        """
        fresh = {
            "accessToken": "already-refreshed-token",
            "refreshToken": "live-refresh",
            "expiresAt": self._FRESH,
        }
        monkeypatch.setattr(
            "agent.anthropic_adapter.read_claude_code_credentials",
            lambda: fresh,
        )

        def _should_not_be_called(*args, **kwargs):  # pragma: no cover - guard
            raise AssertionError("refresh_anthropic_oauth_pure must not be called")

        monkeypatch.setattr(
            "agent.anthropic_adapter.refresh_anthropic_oauth_pure",
            _should_not_be_called,
        )

        # Stale creds passed in by the caller — should be ignored in favor
        # of the live, already-refreshed token.
        result = _refresh_oauth_token({"refreshToken": "stale", "expiresAt": 1})
        assert result == "already-refreshed-token"

    def test_falls_back_to_network_refresh_when_no_fresh_credential(self, monkeypatch):
        """When no live source has a valid token, fall back to refreshing
        ourselves using the freshest available refresh token.
        """
        # Live read returns an expired credential carrying a refresh token.
        monkeypatch.setattr(
            "agent.anthropic_adapter.read_claude_code_credentials",
            lambda: {"accessToken": "expired", "refreshToken": "live-refresh", "expiresAt": 1},
        )
        captured = {}

        def _fake_refresh(refresh_token, **kwargs):
            captured["refresh_token"] = refresh_token
            return {
                "access_token": "newly-minted",
                "refresh_token": "rotated",
                "expires_at_ms": self._FRESH,
            }

        monkeypatch.setattr(
            "agent.anthropic_adapter.refresh_anthropic_oauth_pure", _fake_refresh
        )
        monkeypatch.setattr(
            "agent.anthropic_adapter._write_claude_code_credentials",
            lambda *a, **k: None,
        )

        result = _refresh_oauth_token({"refreshToken": "caller-refresh", "expiresAt": 1})
        assert result == "newly-minted"
        # Prefers the live source's refresh token over the caller's stale copy.
        assert captured["refresh_token"] == "live-refresh"

def test_resolve_anthropic_token_loads_secure_claude_code_env_file(tmp_path, monkeypatch):
    from agent import anthropic_adapter

    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    auth_file = codex_dir / "cc-auth.env"
    auth_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=cc-test-token\n")
    auth_file.chmod(0o600)

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(anthropic_adapter, "read_claude_code_credentials", lambda: None)

    assert anthropic_adapter.resolve_anthropic_token() == "cc-test-token"


def test_resolve_anthropic_token_ignores_world_readable_claude_code_env_file(tmp_path, monkeypatch):
    from agent import anthropic_adapter

    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    auth_file = codex_dir / "cc-auth.env"
    auth_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=cc-leaky-token\n")
    auth_file.chmod(0o644)

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(anthropic_adapter, "read_claude_code_credentials", lambda: None)

    assert anthropic_adapter.resolve_anthropic_token() is None

@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644])
def test_resolve_anthropic_token_rejects_non_owner_private_env_file(tmp_path, monkeypatch, mode):
    """Any group or other permission on the fallback file fails closed."""
    from agent import anthropic_adapter

    auth_file = tmp_path / ".codex" / "cc-auth.env"
    auth_file.parent.mkdir()
    auth_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=file-value\n")
    auth_file.chmod(mode)

    monkeypatch.setattr(anthropic_adapter.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(anthropic_adapter, "read_claude_code_credentials", lambda: None)

    assert anthropic_adapter.resolve_anthropic_token() is None


def test_resolve_anthropic_token_env_beats_private_file_and_is_not_logged(tmp_path, monkeypatch, caplog):
    """An explicit process value wins and neither credential value is logged."""
    from agent import anthropic_adapter

    auth_file = tmp_path / ".codex" / "cc-auth.env"
    auth_file.parent.mkdir()
    file_value = "file-only-value"
    env_value = "explicit-env-value"
    auth_file.write_text(f"CLAUDE_CODE_OAUTH_TOKEN={file_value}\n")
    auth_file.chmod(0o600)

    monkeypatch.setattr(anthropic_adapter.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", env_value)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(anthropic_adapter, "read_claude_code_credentials", lambda: None)

    assert anthropic_adapter.resolve_anthropic_token() == env_value
    assert file_value not in caplog.text
    assert env_value not in caplog.text


def test_resolve_anthropic_token_rejects_non_regular_env_file(tmp_path, monkeypatch):
    """The fallback must be a private regular file, not a directory or device."""
    from agent import anthropic_adapter

    (tmp_path / ".codex" / "cc-auth.env").mkdir(parents=True)
    monkeypatch.setattr(anthropic_adapter.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(anthropic_adapter, "read_claude_code_credentials", lambda: None)

    assert anthropic_adapter.resolve_anthropic_token() is None


def test_claude_code_env_file_rejects_symlink(tmp_path, monkeypatch):
    from agent import anthropic_adapter

    target = tmp_path / "target.env"
    target.write_text("CLAUDE_CODE_OAUTH_TOKEN=symlink-token\n")
    target.chmod(0o600)
    auth_file = tmp_path / ".codex" / "cc-auth.env"
    auth_file.parent.mkdir()
    try:
        auth_file.symlink_to(target)
    except OSError as error:
        pytest.skip(f"symlink creation unavailable: {error}")

    monkeypatch.setattr(anthropic_adapter.Path, "home", lambda: tmp_path)

    assert anthropic_adapter._read_claude_code_oauth_token_env_file() is None


def test_claude_code_env_file_rejects_wrong_owner(tmp_path, monkeypatch):
    import os
    from agent import anthropic_adapter

    if not hasattr(os, "getuid"):
        pytest.skip("owner UID is unavailable on this platform")

    auth_file = tmp_path / ".codex" / "cc-auth.env"
    auth_file.parent.mkdir()
    auth_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=wrong-owner-token\n")
    auth_file.chmod(0o600)

    monkeypatch.setattr(anthropic_adapter.Path, "home", lambda: tmp_path)
    actual_uid = os.getuid()
    monkeypatch.setattr(anthropic_adapter.os, "getuid", lambda: actual_uid + 1)

    assert anthropic_adapter._read_claude_code_oauth_token_env_file() is None


def test_claude_code_env_file_rejects_opened_descriptor_after_path_replacement(
    tmp_path,
    monkeypatch,
):
    import os
    from agent import anthropic_adapter

    auth_file = tmp_path / ".codex" / "cc-auth.env"
    auth_file.parent.mkdir()
    auth_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=opened-token\n")
    auth_file.chmod(0o600)

    replacement = tmp_path / "replacement.env"
    replacement.write_text("CLAUDE_CODE_OAUTH_TOKEN=replacement-token\n")
    replacement.chmod(0o600)

    monkeypatch.setattr(anthropic_adapter.Path, "home", lambda: tmp_path)
    real_open = os.open

    def open_then_replace(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if str(path).endswith("cc-auth.env"):
            os.replace(replacement, auth_file)
        return fd

    monkeypatch.setattr(anthropic_adapter.os, "open", open_then_replace)

    assert anthropic_adapter._read_claude_code_oauth_token_env_file() is None


def test_claude_code_env_file_rejects_fifo_without_blocking(tmp_path, monkeypatch):
    import os
    from agent import anthropic_adapter

    if not hasattr(os, "mkfifo") or not hasattr(os, "O_NONBLOCK"):
        pytest.skip("nonblocking FIFO support is unavailable")

    auth_file = tmp_path / ".codex" / "cc-auth.env"
    auth_file.parent.mkdir()
    os.mkfifo(auth_file, 0o600)

    monkeypatch.setattr(anthropic_adapter.Path, "home", lambda: tmp_path)

    assert anthropic_adapter._read_claude_code_oauth_token_env_file() is None


def test_claude_code_env_file_rejects_writable_parent_directory(
    tmp_path,
    monkeypatch,
):
    from agent import anthropic_adapter

    auth_file = tmp_path / ".codex" / "cc-auth.env"
    auth_file.parent.mkdir(mode=0o777)
    auth_file.parent.chmod(0o777)
    auth_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=parent-writable-token\n")
    auth_file.chmod(0o600)

    monkeypatch.setattr(anthropic_adapter.Path, "home", lambda: tmp_path)

    assert anthropic_adapter._read_claude_code_oauth_token_env_file() is None


def test_claude_code_env_file_rejects_hardlinked_file(tmp_path, monkeypatch):
    import os
    from agent import anthropic_adapter

    victim = tmp_path / "victim.env"
    victim.write_text("CLAUDE_CODE_OAUTH_TOKEN=hardlink-token\n")
    victim.chmod(0o600)
    auth_file = tmp_path / ".codex" / "cc-auth.env"
    auth_file.parent.mkdir()
    auth_file.parent.chmod(0o775)
    os.link(victim, auth_file)

    monkeypatch.setattr(anthropic_adapter.Path, "home", lambda: tmp_path)

    assert anthropic_adapter._read_claude_code_oauth_token_env_file() is None


def test_claude_code_env_file_accepts_export_assignment(tmp_path, monkeypatch):
    from agent import anthropic_adapter

    auth_file = tmp_path / ".codex" / "cc-auth.env"
    auth_file.parent.mkdir()
    auth_file.write_text('  export CLAUDE_CODE_OAUTH_TOKEN="export-token"  \n')
    auth_file.chmod(0o600)

    monkeypatch.setattr(anthropic_adapter.Path, "home", lambda: tmp_path)

    assert (
        anthropic_adapter._read_claude_code_oauth_token_env_file()
        == "export-token"
    )


def test_claude_code_env_file_accepts_group_writable_owner_directory(
    tmp_path,
    monkeypatch,
):
    from agent import anthropic_adapter

    auth_file = tmp_path / ".codex" / "cc-auth.env"
    auth_file.parent.mkdir()
    auth_file.parent.chmod(0o775)
    auth_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=umask-002-token\n")
    auth_file.chmod(0o600)

    monkeypatch.setattr(anthropic_adapter.Path, "home", lambda: tmp_path)

    assert (
        anthropic_adapter._read_claude_code_oauth_token_env_file()
        == "umask-002-token"
    )


@pytest.mark.parametrize(
    ("assignment", "expected"),
    [
        ("CLAUDE_CODE_OAUTH_TOKEN=abc123 # note\n", "abc123"),
        ('CLAUDE_CODE_OAUTH_TOKEN="abc123" # note\n', "abc123"),
        ("\texport\tCLAUDE_CODE_OAUTH_TOKEN=abc123\n", "abc123"),
    ],
)
def test_claude_code_env_file_parses_comments_and_export_whitespace(
    tmp_path,
    monkeypatch,
    assignment,
    expected,
):
    from agent import anthropic_adapter

    auth_file = tmp_path / ".codex" / "cc-auth.env"
    auth_file.parent.mkdir()
    auth_file.write_text(assignment)
    auth_file.chmod(0o600)

    monkeypatch.setattr(anthropic_adapter.Path, "home", lambda: tmp_path)

    assert anthropic_adapter._read_claude_code_oauth_token_env_file() == expected


def test_claude_code_env_file_uses_last_assignment(tmp_path, monkeypatch):
    from agent import anthropic_adapter

    auth_file = tmp_path / ".codex" / "cc-auth.env"
    auth_file.parent.mkdir()
    auth_file.write_text(
        "CLAUDE_CODE_OAUTH_TOKEN=\n"
        "CLAUDE_CODE_OAUTH_TOKEN=last-token\n"
    )
    auth_file.chmod(0o600)

    monkeypatch.setattr(anthropic_adapter.Path, "home", lambda: tmp_path)

    assert (
        anthropic_adapter._read_claude_code_oauth_token_env_file()
        == "last-token"
    )


def test_claude_code_env_file_logs_rejection_reason_without_token(
    tmp_path,
    monkeypatch,
    caplog,
):
    from agent import anthropic_adapter

    auth_file = tmp_path / ".codex" / "cc-auth.env"
    auth_file.parent.mkdir()
    token = "never-log-this-token"
    auth_file.write_text(f"CLAUDE_CODE_OAUTH_TOKEN={token}\n")
    auth_file.chmod(0o644)

    monkeypatch.setattr(anthropic_adapter.Path, "home", lambda: tmp_path)

    with caplog.at_level("DEBUG", logger=anthropic_adapter.__name__):
        assert anthropic_adapter._read_claude_code_oauth_token_env_file() is None

    assert "readable or writable by another user" in caplog.text
    assert token not in caplog.text


def test_claude_code_env_file_fallback_rejects_symlink(tmp_path, monkeypatch):
    from agent import anthropic_adapter

    target = tmp_path / "target.env"
    target.write_text("CLAUDE_CODE_OAUTH_TOKEN=fallback-symlink-token\n")
    target.chmod(0o600)
    auth_file = tmp_path / ".codex" / "cc-auth.env"
    auth_file.parent.mkdir()
    auth_file.symlink_to(target)

    monkeypatch.setattr(anthropic_adapter.Path, "home", lambda: tmp_path)
    monkeypatch.delattr(anthropic_adapter.os, "O_NOFOLLOW", raising=False)

    assert anthropic_adapter._read_claude_code_oauth_token_env_file() is None


def test_claude_code_env_file_fallback_rejects_path_replacement(
    tmp_path,
    monkeypatch,
):
    import os
    from agent import anthropic_adapter

    auth_file = tmp_path / ".codex" / "cc-auth.env"
    auth_file.parent.mkdir()
    auth_file.write_text("CLAUDE_CODE_OAUTH_TOKEN=original-token\n")
    auth_file.chmod(0o600)
    replacement = tmp_path / "replacement.env"
    replacement.write_text("CLAUDE_CODE_OAUTH_TOKEN=replacement-token\n")
    replacement.chmod(0o600)

    monkeypatch.setattr(anthropic_adapter.Path, "home", lambda: tmp_path)
    monkeypatch.delattr(anthropic_adapter.os, "O_NOFOLLOW", raising=False)
    real_open = os.open

    def replace_then_open(path, flags, *args, **kwargs):
        if str(path).endswith("cc-auth.env"):
            os.replace(replacement, auth_file)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(anthropic_adapter.os, "open", replace_then_open)

    assert anthropic_adapter._read_claude_code_oauth_token_env_file() is None
