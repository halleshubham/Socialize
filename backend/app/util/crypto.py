from functools import lru_cache

from cryptography.fernet import Fernet

from backend.app.config import get_settings


@lru_cache
def _fernet() -> Fernet:
    key = get_settings().app_encryption_key
    if not key:
        raise RuntimeError(
            "APP_ENCRYPTION_KEY is not set. Generate one with: "
            "python -c \"from cryptography.fernet import Fernet; "
            'print(Fernet.generate_key().decode())"'
        )
    return Fernet(key.encode("utf-8"))


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
