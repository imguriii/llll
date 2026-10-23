"""Dashboard credentials: separate from Discord passwords and tokens."""
import hashlib
import hmac
import secrets


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600000)
    return salt + ':' + digest.hex()


def verify_password(password, encoded):
    try:
        return hmac.compare_digest(password_hash(password, encoded.split(':')[0]), encoded)
    except (ValueError, TypeError, AttributeError):
        return False


def normalized_username(value):
    return value.strip().lower()
