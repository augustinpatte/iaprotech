from __future__ import annotations

import os

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from app.core.vault import decrypt_and_migrate, decrypt_data, encrypt_data


def _legacy_blob(plaintext: bytes, master_key: bytes) -> bytes:
    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100_000,
    ).derive(master_key)
    ct_tag = AESGCM(key).encrypt(nonce, plaintext, None)
    return salt + nonce + ct_tag[-16:] + ct_tag[:-16]


def test_v2_roundtrip():
    plaintext = b"secret pii payload"
    master_key = b"test-master-key"
    org_id = "org_A"

    blob = encrypt_data(plaintext, master_key, org_id)

    assert blob[0] == 0x02
    assert decrypt_data(blob, master_key, org_id) == plaintext


def test_v1_to_v2_migration():
    plaintext = b"legacy secret pii payload"
    master_key = b"test-master-key"
    org_id = "org_A"
    legacy = _legacy_blob(plaintext, master_key)

    decrypted, migrated = decrypt_and_migrate(legacy, master_key, org_id)

    assert decrypted == plaintext
    assert migrated[0] == 0x02
    assert migrated != legacy
    assert decrypt_data(migrated, master_key, org_id) == plaintext


def test_cross_org_isolation():
    plaintext = b"org-bound secret pii payload"
    master_key = b"test-master-key"
    blob = encrypt_data(plaintext, master_key, "org_A")

    with pytest.raises(InvalidTag):
        decrypt_data(blob, master_key, "org_B")
