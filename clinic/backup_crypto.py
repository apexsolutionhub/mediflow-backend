"""Encrypt backups at rest (docs/OFFLINE_SYNC_CONTRACT.md, section 11.2).

A backup holds every patient record, so it is encrypted before it leaves the database server's reach: on the
second disk and, above all, in off-site storage. The key comes from a passphrase the manager keeps; **without
the passphrase an encrypted backup cannot be read by anyone**, so it must be written down somewhere safe.

File format (version 1), streamed so a large dump never has to fit in memory:

    "MFBK1\\n"  salt(16)  nonce_prefix(8)
    then, for each chunk:  flag(1)  length(4, big endian)  ciphertext+tag

Each chunk is AES-256-GCM with nonce = nonce_prefix + chunk number(4). The chunk number and the flag
("1" on the last chunk) are authenticated, so reordering, dropping or cutting chunks is detected, as is any
changed byte. The key is derived from the passphrase with scrypt.
"""
import os
import struct

MAGIC = b"MFBK1\n"
SALT_LEN = 16
PREFIX_LEN = 8
CHUNK = 1024 * 1024
SCRYPT = dict(n=2**15, r=8, p=1)
MORE, LAST = b"\x00", b"\x01"


class CryptoError(Exception):
    """Wrong passphrase, a damaged or cut-off file, or the encryption library missing."""


def _aes(passphrase: str, salt: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise CryptoError("Encrypted backups need the 'cryptography' package (pip install cryptography).") from exc
    if not passphrase:
        raise CryptoError("This backup is encrypted. Set BACKUP_PASSPHRASE to the passphrase kept with the manager.")
    key = Scrypt(salt=salt, length=32, **SCRYPT).derive(passphrase.encode("utf-8"))
    return AESGCM(key)


def _nonce(prefix: bytes, number: int) -> bytes:
    return prefix + struct.pack(">I", number)


def _aad(number: int, flag: bytes) -> bytes:
    return MAGIC + struct.pack(">I", number) + flag


def is_encrypted(path) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


class EncryptingWriter:
    """A write-only file object that encrypts what is written to it into `raw` (a binary file)."""

    def __init__(self, raw, passphrase: str):
        salt, self._prefix = os.urandom(SALT_LEN), os.urandom(PREFIX_LEN)
        self._aes = _aes(passphrase, salt)
        self._raw = raw
        self._raw.write(MAGIC + salt + self._prefix)
        self._buffer = bytearray()
        self._number = 0
        self._closed = False

    def _emit(self, plain: bytes, last: bool):
        flag = LAST if last else MORE
        sealed = self._aes.encrypt(_nonce(self._prefix, self._number), plain, _aad(self._number, flag))
        self._raw.write(flag + struct.pack(">I", len(sealed)) + sealed)
        self._number += 1

    def write(self, data) -> int:
        self._buffer += data
        # Hold back the last full chunk: only at close do we know whether it is the final one.
        while len(self._buffer) > CHUNK:
            self._emit(bytes(self._buffer[:CHUNK]), last=False)
            del self._buffer[:CHUNK]
        return len(data)

    def flush(self):
        pass

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._emit(bytes(self._buffer), last=True)  # may be empty: an empty file is one empty final chunk
        self._buffer.clear()
        self._raw.flush()


class DecryptingReader:
    """A read-only file object that decrypts `raw` (a binary file) as it is read."""

    def __init__(self, raw, passphrase: str):
        header = raw.read(len(MAGIC) + SALT_LEN + PREFIX_LEN)
        if len(header) != len(MAGIC) + SALT_LEN + PREFIX_LEN or header[: len(MAGIC)] != MAGIC:
            raise CryptoError("This is not an encrypted MediFlow backup.")
        salt = header[len(MAGIC) : len(MAGIC) + SALT_LEN]
        self._prefix = header[len(MAGIC) + SALT_LEN :]
        self._aes = _aes(passphrase, salt)
        self._raw = raw
        self._buffer = b""
        self._number = 0
        self._finished = False

    def _next_chunk(self):
        head = self._raw.read(5)
        if not head:
            raise CryptoError("The backup is cut off: it ends before its last block.")
        if len(head) != 5:
            raise CryptoError("The backup is damaged.")
        flag, (length,) = head[:1], struct.unpack(">I", head[1:])
        sealed = self._raw.read(length)
        if flag not in (MORE, LAST) or len(sealed) != length:
            raise CryptoError("The backup is damaged or cut off.")
        try:
            plain = self._aes.decrypt(_nonce(self._prefix, self._number), sealed, _aad(self._number, flag))
        except Exception as exc:  # cryptography raises InvalidTag
            raise CryptoError("Wrong passphrase, or the backup was changed or damaged.") from exc
        self._number += 1
        if flag == LAST:
            self._finished = True
            if self._raw.read(1):
                raise CryptoError("The backup has extra data after its end.")
        return plain

    def read(self, size=-1) -> bytes:
        while (size < 0 or len(self._buffer) < size) and not self._finished:
            self._buffer += self._next_chunk()
        if size < 0:
            data, self._buffer = self._buffer, b""
        else:
            data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data

    def readable(self) -> bool:
        return True

    def close(self):
        self._raw.close()
