# -*- coding: utf-8 -*-
"""Шифрование резервных копий: AES-256-GCM, ключ из пароля по scrypt (N=2^15, r=8, p=1).

Формат файла: «KORREKTOR-ENC1\\n» (15 байт) + соль (16) + nonce (12) + шифротекст + тег GCM (16).
Шифрование и расшифровка — потоком, без загрузки архива в память. Неверный пароль или подмена хотя бы одного байта
обнаруживаются проверкой тега: расшифрованный файл в этом случае удаляется.
"""
import os
import secrets

MAGIC = b"KORREKTOR-ENC1\n"
CHUNK = 1 << 20


class CryptoError(ValueError):
    pass


def _lib():
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ImportError:
        raise CryptoError("для шифрования копий нужен пакет cryptography (pip install cryptography)")
    return Cipher, algorithms, modes, Scrypt


def _key(password, salt):
    *_, Scrypt = _lib()
    return Scrypt(salt=salt, length=32, n=2 ** 15, r=8, p=1).derive(password.encode("utf-8"))


def is_encrypted(path):
    try:
        with open(path, "rb") as fh:
            return fh.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def encrypt_file(src, dst, password):
    if not password:
        raise CryptoError("не задан пароль резервных копий")
    Cipher, algorithms, modes, _ = _lib()
    salt, nonce = secrets.token_bytes(16), secrets.token_bytes(12)
    enc = Cipher(algorithms.AES(_key(password, salt)), modes.GCM(nonce)).encryptor()
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        fo.write(MAGIC + salt + nonce)
        for chunk in iter(lambda: fi.read(CHUNK), b""):
            fo.write(enc.update(chunk))
        fo.write(enc.finalize())
        fo.write(enc.tag)


def decrypt_file(src, dst, password):
    """Расшифровывает src в dst. CryptoError — неверный пароль или файл поврежден (dst удаляется)."""
    if not password:
        raise CryptoError("копия зашифрована, а пароль резервных копий не задан (Настройки → «Система»)")
    Cipher, algorithms, modes, _ = _lib()
    size = os.path.getsize(src)
    head = len(MAGIC) + 16 + 12
    if size < head + 16:
        raise CryptoError("файл зашифрованной копии поврежден (слишком короткий)")
    with open(src, "rb") as fi:
        if fi.read(len(MAGIC)) != MAGIC:
            raise CryptoError("это не зашифрованная копия Корректора")
        salt, nonce = fi.read(16), fi.read(12)
        fi.seek(size - 16)
        tag = fi.read(16)
        fi.seek(head)
        dec = Cipher(algorithms.AES(_key(password, salt)), modes.GCM(nonce, tag)).decryptor()
        left = size - head - 16
        try:
            with open(dst, "wb") as fo:
                while left > 0:
                    chunk = fi.read(min(CHUNK, left))
                    if not chunk:
                        break
                    left -= len(chunk)
                    fo.write(dec.update(chunk))
                fo.write(dec.finalize())
        except Exception as e:  # noqa — InvalidTag и ошибки чтения
            try:
                os.remove(dst)
            except OSError:
                pass
            if e.__class__.__name__ == "InvalidTag":
                raise CryptoError("неверный пароль резервной копии или файл поврежден")
            raise
