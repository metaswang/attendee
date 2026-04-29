import base64

from cryptography.hazmat.primitives import hashes, hmac as crypto_hmac


def sign_message_with_hmac_sha256(message: bytes, secret: bytes | str) -> str:
    if isinstance(secret, str):
        secret = secret.encode("utf-8")
    h = crypto_hmac.HMAC(secret, hashes.SHA256())
    h.update(message)
    digest = h.finalize()
    return base64.b64encode(digest).decode("utf-8")
