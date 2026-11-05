"""
tests/test_token_crypto.py

Round-trip + failure-mode coverage for product/token_crypto.py.

The module reads TOKEN_ENCRYPTION_KEY at import time and fails closed
if it's missing or malformed. Tests 6 and 7 exercise that fail-closed
path by mutating the env and reloading the module.
"""

import importlib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest

from cryptography.fernet import Fernet

import product.token_crypto as token_crypto


def test_encrypt_decrypt_round_trip():
    """Plaintext encrypted then decrypted with the same key returns to itself."""
    plaintext = "hello"
    ciphertext = token_crypto.encrypt(plaintext)
    assert token_crypto.decrypt(ciphertext) == plaintext


def test_encrypt_returns_str():
    """encrypt() returns str, not bytes — token_store stores it as a JSON string."""
    out = token_crypto.encrypt("hello")
    assert isinstance(out, str)
    assert out.startswith("gAAAAA")


def test_decrypt_invalid_token_raises():
    """A garbage string fails to decrypt with InvalidToken."""
    with pytest.raises(token_crypto.InvalidToken):
        token_crypto.decrypt("not_a_real_ciphertext")


def test_decrypt_wrong_key_raises():
    """Ciphertext encrypted with key A cannot be decrypted with key B."""
    other_key = Fernet.generate_key()
    other_fernet = Fernet(other_key)
    other_ciphertext = other_fernet.encrypt(b"hello").decode("ascii")
    with pytest.raises(token_crypto.InvalidToken):
        token_crypto.decrypt(other_ciphertext)


def test_encrypt_non_str_input_raises():
    """encrypt() rejects non-str input. token_store always passes str."""
    with pytest.raises(TypeError):
        token_crypto.encrypt(123)


def test_missing_env_var_fails_module_load(monkeypatch):
    """Module import with TOKEN_ENCRYPTION_KEY unset raises RuntimeError."""
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
    sys.modules.pop("product.token_crypto", None)
    with pytest.raises(RuntimeError, match="TOKEN_ENCRYPTION_KEY"):
        importlib.import_module("product.token_crypto")
    # Restore the test key for subsequent tests by reloading with conftest's key
    sys.modules.pop("product.token_crypto", None)
    monkeypatch.setenv(
        "TOKEN_ENCRYPTION_KEY",
        "DdTesYfAw9ieKl1973x-OoRZMALZTv4jLR3Jctt0gKc=",
    )
    importlib.import_module("product.token_crypto")


def test_malformed_env_var_fails_module_load(monkeypatch):
    """Module import with a non-Fernet key raises RuntimeError."""
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "not_a_valid_key")
    sys.modules.pop("product.token_crypto", None)
    with pytest.raises(RuntimeError, match="TOKEN_ENCRYPTION_KEY"):
        importlib.import_module("product.token_crypto")
    # Restore the test key for subsequent tests
    sys.modules.pop("product.token_crypto", None)
    monkeypatch.setenv(
        "TOKEN_ENCRYPTION_KEY",
        "DdTesYfAw9ieKl1973x-OoRZMALZTv4jLR3Jctt0gKc=",
    )
    importlib.import_module("product.token_crypto")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
