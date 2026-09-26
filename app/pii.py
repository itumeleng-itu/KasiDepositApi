"""Personal numbers at rest: SA ID numbers and bank account numbers.

Neither is ever written to the database as plaintext. Each is stored as

- ciphertext: AES-256-GCM, a fresh 12-byte nonce per value, stored as
  nonce || ciphertext+tag. Kept because a verified identity (FICA) and a
  payout account must be recoverable; decrypted only where it is needed (an
  account number, at the moment a payout is instructed).
- for ID numbers, also a keyed hash: HMAC-SHA256, so "is this ID already
  registered?" is one indexed lookup without decrypting every row.

Both keys are derived with HKDF from one secret, PII_KEY, which lives only in
the environment (never in the database or the repo). The database alone, a
backup or a leaked dump, therefore reveals no ID or account number. Losing
PII_KEY makes the ciphertexts unrecoverable, so it must be backed up with the
same care as the database password; changing it needs a re-encryption
migration.

The `associated_data` binds each ciphertext to what it is ("sa_id",
"account_number"), so one cannot be swapped in for the other.
"""

import hashlib
import hmac
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

NONCE_BYTES = 12
SA_ID = b"sa_id"
ACCOUNT_NUMBER = b"account_number"


def _derive(secret: str, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=info).derive(
        secret.encode()
    )


class PiiCipher:
    def __init__(self, secret: str) -> None:
        if len(secret) < 32:
            raise ValueError("PII_KEY must be at least 32 characters")
        self._aead = AESGCM(_derive(secret, b"kasideposit/pii/encrypt/v1"))
        self._mac_key = _derive(secret, b"kasideposit/pii/lookup-hash/v1")

    def __repr__(self) -> str:  # never show key material
        return "PiiCipher(<redacted>)"

    def encrypt(self, plaintext: str, kind: bytes) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        return nonce + self._aead.encrypt(nonce, plaintext.encode(), kind)

    def decrypt(self, blob: bytes, kind: bytes) -> str:
        nonce, sealed = blob[:NONCE_BYTES], blob[NONCE_BYTES:]
        return self._aead.decrypt(nonce, sealed, kind).decode()

    def lookup_hash(self, plaintext: str, kind: bytes) -> str:
        return hmac.new(self._mac_key, kind + b":" + plaintext.encode(), hashlib.sha256).hexdigest()
