"""Tests for the AES-GCM blob helpers used by the multi-user store."""

import base64
import os

import pytest
from cryptography.exceptions import InvalidTag

from tp_mcp.auth.encrypted import _env_key, decrypt_blob, encrypt_blob


def test_roundtrip_default_key():
    blob = encrypt_blob("hello-cookie")
    assert isinstance(blob, bytes)
    assert decrypt_blob(blob) == "hello-cookie"


def test_roundtrip_explicit_key():
    key = os.urandom(32)
    blob = encrypt_blob("secret", key)
    assert decrypt_blob(blob, key) == "secret"


def test_wrong_key_fails():
    blob = encrypt_blob("secret", os.urandom(32))
    with pytest.raises(InvalidTag):
        decrypt_blob(blob, os.urandom(32))


def test_nonce_is_random():
    a = encrypt_blob("same", os.urandom(32))
    b = encrypt_blob("same", os.urandom(32))
    assert a != b  # different keys/nonces -> different ciphertext


def test_env_key_override(monkeypatch):
    key = os.urandom(32)
    monkeypatch.setenv("TP_MCP_ENC_KEY", base64.b64encode(key).decode())
    assert _env_key() == key
    # encrypt with env key, decrypt with the same explicit key
    blob = encrypt_blob("v")
    assert decrypt_blob(blob, key) == "v"


def test_env_key_invalid_length_ignored(monkeypatch):
    monkeypatch.setenv("TP_MCP_ENC_KEY", base64.b64encode(b"tooshort").decode())
    assert _env_key() is None
