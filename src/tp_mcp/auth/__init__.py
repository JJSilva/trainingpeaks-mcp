"""Authentication module for TrainingPeaks MCP Server."""

from tp_mcp.auth.encrypted import EncryptedCredentialStore, decrypt_blob, encrypt_blob
from tp_mcp.auth.keyring import CredentialResult, is_keyring_available
from tp_mcp.auth.multiuser_store import (
    clear_user_cookie,
    get_user_cookie,
    get_user_meta,
    store_user_cookie,
)
from tp_mcp.auth.storage import (
    clear_credential,
    get_credential,
    get_storage_backend,
    store_credential,
)
from tp_mcp.auth.validator import AuthResult, AuthStatus, validate_auth, validate_auth_sync

__all__ = [
    "AuthResult",
    "AuthStatus",
    "CredentialResult",
    "EncryptedCredentialStore",
    "clear_credential",
    "clear_user_cookie",
    "decrypt_blob",
    "encrypt_blob",
    "get_credential",
    "get_storage_backend",
    "get_user_cookie",
    "get_user_meta",
    "is_keyring_available",
    "store_credential",
    "store_user_cookie",
    "validate_auth",
    "validate_auth_sync",
]
