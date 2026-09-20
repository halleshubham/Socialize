from functools import lru_cache

from cryptography.fernet import Fernet, MultiFernet

from backend.app.config import get_settings


@lru_cache
def _fernet() -> MultiFernet:
    """MultiFernet, not a single Fernet - makes key rotation possible (see
    config.py's app_encryption_key_previous docstring). Encryption always
    uses the FIRST key (app_encryption_key, the current/primary one, per
    MultiFernet's own documented behavior); decryption tries every key in
    order, so ciphertext produced under an older key - anything encrypted
    before a rotation - still decrypts correctly without needing every
    existing UserApiKey/OAuthCredential/token row to be re-encrypted in
    place. A single-key deployment (app_encryption_key_previous unset,
    the default) behaves identically to before - MultiFernet with one key
    is not meaningfully different from using that key directly."""
    settings = get_settings()
    if not settings.app_encryption_key:
        raise RuntimeError(
            "APP_ENCRYPTION_KEY is not set. Generate one with: "
            "python -c \"from cryptography.fernet import Fernet; "
            'print(Fernet.generate_key().decode())"'
        )
    keys = [settings.app_encryption_key]
    keys += [k.strip() for k in settings.app_encryption_key_previous.split(",") if k.strip()]
    return MultiFernet([Fernet(k.encode("utf-8")) for k in keys])


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
