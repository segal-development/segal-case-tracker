"""MultiFernet encryption tests — zero-downtime key rotation.

Encrypt/rotate with the primary key; decrypt with the primary OR any fallback.
This lets a key rotation keep decrypting old ciphertext while new writes use the
new key, with no downtime.
"""

import pytest
from cryptography.fernet import Fernet

from app.config import _build_multifernet

NEW = Fernet.generate_key().decode()
OLD = Fernet.generate_key().decode()


def test_roundtrip_primary_only():
    mf = _build_multifernet(NEW, [], strict=True)
    assert mf.decrypt(mf.encrypt(b"secret")) == b"secret"


def test_decrypts_old_ciphertext_via_fallback():
    """Ciphertext made with the OLD key still decrypts while OLD is a fallback."""
    old_ct = Fernet(OLD.encode()).encrypt(b"pjudpass")
    mf = _build_multifernet(NEW, [OLD], strict=True)  # NEW primary, OLD fallback
    assert mf.decrypt(old_ct) == b"pjudpass"


def test_decrypts_new_ciphertext_too():
    mf = _build_multifernet(NEW, [OLD], strict=True)
    assert mf.decrypt(mf.encrypt(b"x")) == b"x"


def test_encrypt_uses_primary_new_key():
    """New writes are encrypted with the PRIMARY (new) key, not a fallback."""
    mf = _build_multifernet(NEW, [OLD], strict=True)
    ct = mf.encrypt(b"y")
    # Decryptable with NEW alone → proves the primary key was used.
    assert Fernet(NEW.encode()).decrypt(ct) == b"y"


def test_old_only_cannot_decrypt_new_ciphertext():
    """Sanity: a new-key ciphertext is NOT decryptable by the old key alone."""
    mf_new = _build_multifernet(NEW, [], strict=True)
    new_ct = mf_new.encrypt(b"z")
    with pytest.raises(Exception):
        Fernet(OLD.encode()).decrypt(new_ct)


def test_strict_rejects_bad_fallback():
    with pytest.raises(Exception):
        _build_multifernet(NEW, ["not-a-real-fernet-key"], strict=True)


def test_lenient_pads_bad_fallback_in_dev():
    """Dev mode (strict=False) still builds (padded) — no crash for local work."""
    mf = _build_multifernet(NEW, ["weakdevkey"], strict=False)
    assert mf.decrypt(mf.encrypt(b"d")) == b"d"
