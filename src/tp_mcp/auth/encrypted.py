"""Encrypted file-based credential storage for TrainingPeaks authentication.

This is a fallback for environments where system keyring is not available
(headless servers, containers, etc.).

Limitations:
- Without a user password, encryption is bound to machine identity only.
  Anyone with access to the machine and this code can derive the key.
  For stronger protection, pass a password to EncryptedCredentialStore.
"""

import base64
import contextlib
import hashlib
import os
import platform
import stat
from pathlib import Path

from cryptography.hazmat.primitives import hashes as crypto_hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from tp_mcp.auth.keyring import CredentialResult

# Storage location
CONFIG_DIR = Path.home() / ".config" / "trainingpeaks-mcp"
CREDENTIALS_FILE = CONFIG_DIR / "credentials.enc"


def _get_machine_id() -> bytes:
    """Get a machine-specific identifier for key derivation.

    This combines several system identifiers to create a unique machine fingerprint.
    Not cryptographically secure on its own, but adds a layer of machine-binding.

    Returns:
        Machine identifier bytes.
    """
    components = [
        platform.node(),  # hostname
        platform.machine(),  # CPU architecture
        platform.system(),  # OS name
    ]

    # Try to get more stable machine identifiers
    try:
        # macOS: hardware UUID
        if platform.system() == "Darwin":
            import subprocess

            result = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if "IOPlatformUUID" in result.stdout:
                for line in result.stdout.split("\n"):
                    if "IOPlatformUUID" in line:
                        uuid = line.split("=")[-1].strip().strip('"')
                        components.append(uuid)
                        break
    except Exception:
        pass

    try:
        # Linux: machine-id
        machine_id_path = Path("/etc/machine-id")
        if machine_id_path.exists():
            components.append(machine_id_path.read_text().strip())
    except Exception:
        pass

    return "|".join(components).encode("utf-8")


_KDF_ITERATIONS = 600_000


def _env_key() -> bytes | None:
    """Return a server-provided 32-byte key from ``TP_MCP_ENC_KEY`` if set.

    On ephemeral/hosted hosts the machine-id binding is unstable across redeploys,
    so a deployment can pin a stable base64-encoded 32-byte key via the
    ``TP_MCP_ENC_KEY`` environment variable. Only used by the multi-user store's
    blob helpers; the local file store keeps machine-id binding unless a password
    is supplied.
    """
    raw = os.environ.get("TP_MCP_ENC_KEY")
    if not raw:
        return None
    try:
        key = base64.b64decode(raw)
    except Exception:
        return None
    return key if len(key) == 32 else None


def _derive_key(password: str | None = None) -> bytes:
    """Derive an encryption key using PBKDF2-HMAC-SHA256.

    Uses the machine ID as salt and an optional password as key material.

    Args:
        password: Optional user password for additional security.

    Returns:
        32-byte key for AES-256.
    """
    machine_id = _get_machine_id()
    key_material = password.encode("utf-8") if password else b"tp-mcp-default"
    kdf = PBKDF2HMAC(
        algorithm=crypto_hashes.SHA256(),
        length=32,
        salt=machine_id,
        iterations=_KDF_ITERATIONS,
    )
    return kdf.derive(key_material)


def encrypt_blob(plaintext: str, key: bytes | None = None) -> bytes:
    """Encrypt a string with AES-256-GCM, returning ``nonce || ciphertext``.

    Args:
        plaintext: The value to encrypt.
        key: Optional 32-byte key. Defaults to ``TP_MCP_ENC_KEY`` if set, else the
            machine-derived key.

    Returns:
        Raw bytes of ``nonce (12) || ciphertext`` (not base64-encoded).
    """
    if key is None:
        key = _env_key() or _derive_key()
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return nonce + ciphertext


def decrypt_blob(blob: bytes, key: bytes | None = None) -> str:
    """Decrypt bytes produced by :func:`encrypt_blob`.

    Args:
        blob: Raw ``nonce || ciphertext`` bytes.
        key: Optional 32-byte key. Defaults to ``TP_MCP_ENC_KEY`` if set, else the
            machine-derived key.

    Returns:
        The decrypted string.
    """
    if key is None:
        key = _env_key() or _derive_key()
    nonce, ciphertext = blob[:12], blob[12:]
    return AESGCM(key).decrypt(nonce, ciphertext, None).decode("utf-8")


def _derive_key_legacy(password: str | None = None) -> bytes:
    """Legacy key derivation (SHA-256). Kept for migration only.

    Args:
        password: Optional user password for additional security.

    Returns:
        32-byte key for AES-256.
    """
    machine_id = _get_machine_id()
    salt = b"trainingpeaks-mcp-v1"
    key_material = (machine_id + password.encode("utf-8")) if password else machine_id
    return hashlib.sha256(salt + key_material).digest()


def _ensure_secure_directory() -> None:
    """Ensure the config directory exists with secure permissions."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    # Set directory permissions to 700 (owner only). Windows may not support chmod.
    with contextlib.suppress(OSError):
        os.chmod(CONFIG_DIR, stat.S_IRWXU)


def _set_file_permissions(path: Path) -> None:
    """Set secure file permissions (600 - owner read/write only)."""
    # Windows may not support chmod
    with contextlib.suppress(OSError):
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


class EncryptedCredentialStore:
    """Encrypted file-based credential storage.

    Uses AES-256-GCM for authenticated encryption with a key derived from
    machine-specific identifiers and an optional user password.
    """

    def __init__(self, password: str | None = None):
        """Initialize the encrypted credential store.

        Args:
            password: Optional password for additional security.
        """
        self._password = password
        self._key = _derive_key(password)
        self._legacy_key = _derive_key_legacy(password)

    def store(self, cookie: str) -> CredentialResult:
        """Store the TrainingPeaks auth cookie in an encrypted file.

        Args:
            cookie: The Production_tpAuth cookie value.

        Returns:
            CredentialResult with success status.
        """
        if not cookie or not cookie.strip():
            return CredentialResult(success=False, message="Cookie value cannot be empty")

        try:
            _ensure_secure_directory()

            # Store nonce + ciphertext, base64 encoded (uses this store's key,
            # not the env/machine default, to preserve password-bound behavior).
            encrypted_data = base64.b64encode(encrypt_blob(cookie.strip(), self._key))
            CREDENTIALS_FILE.write_bytes(encrypted_data)

            _set_file_permissions(CREDENTIALS_FILE)

            return CredentialResult(success=True, message="Credential stored in encrypted file")

        except Exception as e:
            return CredentialResult(success=False, message=f"Encryption error ({type(e).__name__})")

    def get(self) -> CredentialResult:
        """Retrieve the TrainingPeaks auth cookie from the encrypted file.

        Tries the current PBKDF2 key first, then falls back to the legacy
        SHA-256 key and auto-migrates on success.

        Returns:
            CredentialResult with cookie if found.
        """
        if not CREDENTIALS_FILE.exists():
            return CredentialResult(success=False, message="No credential file found")

        encrypted_data = base64.b64decode(CREDENTIALS_FILE.read_bytes())

        # Try new key first
        try:
            cookie = decrypt_blob(encrypted_data, self._key)
            return CredentialResult(success=True, message="Credential retrieved", cookie=cookie)
        except Exception:
            pass

        # Fall back to legacy key and auto-migrate
        try:
            cookie = decrypt_blob(encrypted_data, self._legacy_key)
            self.store(cookie)  # Re-encrypt with new key
            return CredentialResult(
                success=True,
                message="Credential retrieved and migrated to stronger encryption",
                cookie=cookie,
            )
        except Exception:
            return CredentialResult(
                success=False,
                message="Decryption failed. Run 'tp-mcp auth' to re-authenticate.",
            )

    def clear(self) -> CredentialResult:
        """Remove the encrypted credential file.

        Returns:
            CredentialResult with success status.
        """
        try:
            CREDENTIALS_FILE.unlink(missing_ok=True)
            return CredentialResult(success=True, message="Credential file removed")
        except Exception as e:
            return CredentialResult(success=False, message=f"Error removing file ({type(e).__name__})")


# Module-level functions for consistency with keyring interface
_default_store: EncryptedCredentialStore | None = None


def _get_store() -> EncryptedCredentialStore:
    """Get or create the default encrypted store."""
    global _default_store
    if _default_store is None:
        _default_store = EncryptedCredentialStore()
    return _default_store


def store_credential_encrypted(cookie: str) -> CredentialResult:
    """Store credential using encrypted file storage."""
    return _get_store().store(cookie)


def get_credential_encrypted() -> CredentialResult:
    """Get credential from encrypted file storage."""
    return _get_store().get()


def clear_credential_encrypted() -> CredentialResult:
    """Clear credential from encrypted file storage."""
    return _get_store().clear()
