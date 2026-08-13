"""Утилиты безопасности и генерации ключей."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Literal

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


HashAlgorithm = Literal['sha256', 'sha384', 'sha512']


def hash_api_token(
    token: str,
    algorithm: HashAlgorithm = 'sha256',
    *,
    hmac_secret: str | None = None,
) -> str:
    """Возвращает хеш токена в формате hex.

    If ``hmac_secret`` is provided, uses HMAC with the given secret key
    (recommended for production). Otherwise falls back to plain hash
    (backward-compatible).
    """
    normalized = (algorithm or 'sha256').lower()
    if normalized not in {'sha256', 'sha384', 'sha512'}:
        raise ValueError(f'Unsupported hash algorithm: {algorithm}')

    token_bytes = token.encode('utf-8')

    if hmac_secret:
        return hmac.new(hmac_secret.encode('utf-8'), token_bytes, normalized).hexdigest()

    digest = getattr(hashlib, normalized)
    return digest(token_bytes).hexdigest()


def generate_api_token(length: int = 48) -> str:
    """Генерирует криптографически стойкий токен."""
    length = max(24, min(length, 128))
    return secrets.token_urlsafe(length)


def hmac_fingerprint(value: str, *, secret: str, purpose: str) -> str:
    """Return a purpose-separated non-reversible operational fingerprint."""

    if not secret:
        raise ValueError('A configured application secret is required')
    return hmac.new(secret.encode(), f'{purpose}\x00{value}'.encode(), hashlib.sha256).hexdigest()


def _restricted_key(secret: str, purpose: str) -> bytes:
    if not secret:
        raise ValueError('A configured application secret is required')
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b'teplo-entitlement-restricted-v1',
        info=purpose.encode(),
    ).derive(secret.encode())


def encrypt_restricted_identifier(value: str, *, secret: str, purpose: str) -> bytes:
    """Encrypt a cleanup-only identifier without logging or serializing it."""

    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(_restricted_key(secret, purpose)).encrypt(nonce, value.encode(), purpose.encode())
    return b'v1' + nonce + ciphertext


def decrypt_restricted_identifier(payload: bytes, *, secret: str, purpose: str) -> str:
    """Decrypt a cleanup target inside the restricted backend worker only."""

    if not payload.startswith(b'v1') or len(payload) < 31:
        raise ValueError('Unsupported restricted payload')
    clear = AESGCM(_restricted_key(secret, purpose)).decrypt(payload[2:14], payload[14:], purpose.encode())
    return clear.decode()


__all__ = [
    'HashAlgorithm',
    'decrypt_restricted_identifier',
    'encrypt_restricted_identifier',
    'generate_api_token',
    'hash_api_token',
    'hmac_fingerprint',
]
