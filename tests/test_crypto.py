"""
app/crypto.py is the whole reason the README can claim "a DB leak alone
doesn't hand out live Google Calendar access" — google_refresh_token is
encrypted at rest with Fernet, not stored plaintext. These tests exercise
the module directly (no HTTP, no DB) since that claim lives entirely in
this ~25-line module.
"""

from cryptography.fernet import Fernet, InvalidToken

from app import crypto


def test_encrypt_decrypt_round_trip():
    plaintext = "1//0g-some-real-looking-google-refresh-token"
    token = crypto.encrypt(plaintext)
    assert token != plaintext  # never stored/transmitted as plaintext
    assert crypto.decrypt(token) == plaintext


def test_encrypted_value_is_not_a_substring_of_the_plaintext():
    plaintext = "super-secret-refresh-token"
    token = crypto.encrypt(plaintext)
    assert plaintext not in token


def test_decrypting_with_a_different_key_fails_distinctly():
    # The actual security property under test: someone who steals only the
    # database (ciphertext) and not the separately-held
    # TOKEN_ENCRYPTION_KEY cannot recover the refresh token, even if they
    # guess/have *a* Fernet key — decryption must fail loudly, not return
    # garbage silently.
    plaintext = "1//0g-some-real-looking-google-refresh-token"
    token = crypto.encrypt(plaintext)

    other_fernet = Fernet(Fernet.generate_key())
    try:
        other_fernet.decrypt(token.encode())
        raised = False
    except InvalidToken:
        raised = True
    assert raised


def test_encrypt_raises_clear_error_when_key_not_configured(monkeypatch):
    monkeypatch.setattr(crypto, "_fernet", None)
    try:
        crypto.encrypt("anything")
        raised = False
    except RuntimeError as exc:
        raised = True
        assert "TOKEN_ENCRYPTION_KEY" in str(exc)
    assert raised


def test_decrypt_raises_clear_error_when_key_not_configured(monkeypatch):
    monkeypatch.setattr(crypto, "_fernet", None)
    try:
        crypto.decrypt("anything")
        raised = False
    except RuntimeError as exc:
        raised = True
        assert "TOKEN_ENCRYPTION_KEY" in str(exc)
    assert raised


def test_same_plaintext_encrypts_differently_each_time():
    # Fernet includes a random IV/nonce per call — two encryptions of the
    # same refresh token must not be identical ciphertexts (no ECB-style
    # determinism that would let an attacker correlate tokens).
    plaintext = "same-token-both-times"
    first = crypto.encrypt(plaintext)
    second = crypto.encrypt(plaintext)
    assert first != second
    assert crypto.decrypt(first) == crypto.decrypt(second) == plaintext
