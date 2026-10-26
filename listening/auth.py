"""Single-owner app authentication. No credentials are written to the database."""
import hashlib
import hmac
import os
import secrets


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError('12자 이상의 앱 비밀번호를 사용해 주세요.')
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600_000).hex()
    return f'pbkdf2_sha256$600000${salt}${digest}'


def valid_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt, expected = encoded.split('$')
        count = int(rounds)
        if algorithm != 'pbkdf2_sha256' or not 100_000 <= count <= 2_000_000:
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), count).hex()
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def setting(name: str, default=''):
    value = os.getenv(name)
    if value is not None:
        return value
    import streamlit as st
    try:
        return st.secrets.get(name, default)
    except (FileNotFoundError, KeyError):
        return default
