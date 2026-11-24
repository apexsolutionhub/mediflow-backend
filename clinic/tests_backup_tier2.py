"""Offline-sync phase 7: encrypted backups and the off-site copy. No database needed."""
import datetime as dt
import gzip
import hashlib
import os
import struct
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, override_settings

from clinic import backup, backup_crypto, offsite
from clinic.tests_backup import GOOD, NOW, dump_of

PASS = "correct horse battery staple"
SQL = b"".join(GOOD)


def encrypt(data: bytes, passphrase=PASS) -> bytes:
    import io

    raw = io.BytesIO()
    writer = backup_crypto.EncryptingWriter(raw, passphrase)
    writer.write(data)
    writer.close()
    return raw.getvalue()


def decrypt(blob: bytes, passphrase=PASS) -> bytes:
    import io

    return backup_crypto.DecryptingReader(io.BytesIO(blob), passphrase).read()


class CryptoTests(SimpleTestCase):
    def test_round_trip_small_empty_and_large(self):
        self.assertEqual(decrypt(encrypt(SQL)), SQL)
        self.assertEqual(decrypt(encrypt(b"")), b"")
        big = os.urandom(2_600_000)  # three chunks
        self.assertEqual(decrypt(encrypt(big)), big)

    def test_the_file_does_not_contain_the_plain_text(self):
        blob = encrypt(SQL)
        self.assertNotIn(b"CREATE TABLE", blob)
        self.assertTrue(blob.startswith(backup_crypto.MAGIC))

    def test_two_encryptions_of_the_same_data_differ(self):
        self.assertNotEqual(encrypt(SQL), encrypt(SQL))  # fresh salt and nonce every time

    def test_a_wrong_passphrase_is_refused(self):
        with self.assertRaisesMessage(backup_crypto.CryptoError, "Wrong passphrase"):
            decrypt(encrypt(SQL), "not the passphrase")

    def test_no_passphrase_says_what_to_do(self):
        with self.assertRaisesMessage(backup_crypto.CryptoError, "BACKUP_PASSPHRASE"):
            decrypt(encrypt(SQL), "")

    def test_any_changed_byte_is_detected(self):
        blob = bytearray(encrypt(os.urandom(200_000)))
        blob[len(blob) // 2] ^= 0x01
        with self.assertRaises(backup_crypto.CryptoError):
            decrypt(bytes(blob))

    def test_a_cut_off_file_is_detected_even_at_a_chunk_boundary(self):
        blob = encrypt(os.urandom(2_600_000))  # chunks: 1 MiB, 1 MiB, then the final one
        first = 30 + 5 + struct.unpack(">I", blob[31:35])[0]  # end of chunk 0 (header is 30 bytes)
        with self.assertRaisesMessage(backup_crypto.CryptoError, "cut off"):
            decrypt(blob[:first])
        with self.assertRaises(backup_crypto.CryptoError):
            decrypt(blob[:-10])

    def test_reordered_chunks_are_detected(self):
        blob = encrypt(os.urandom(2_600_000))
        start = 30
        length0 = struct.unpack(">I", blob[start + 1 : start + 5])[0]
        end0 = start + 5 + length0
        length1 = struct.unpack(">I", blob[end0 + 1 : end0 + 5])[0]
        end1 = end0 + 5 + length1
        swapped = blob[:start] + blob[end0:end1] + blob[start:end0] + blob[end1:]
        with self.assertRaises(backup_crypto.CryptoError):
            decrypt(swapped)

    def test_extra_data_after_the_end_is_detected(self):
        with self.assertRaisesMessage(backup_crypto.CryptoError, "extra data"):
            decrypt(encrypt(SQL) + b"x")

    def test_detecting_an_encrypted_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            enc, plain = Path(tmp) / "a.enc", Path(tmp) / "b.gz"
            enc.write_bytes(encrypt(SQL))
            with gzip.open(plain, "wb") as handle:
                handle.write(SQL)
            self.assertTrue(backup_crypto.is_encrypted(enc))
            self.assertFalse(backup_crypto.is_encrypted(plain))
            self.assertFalse(backup_crypto.is_encrypted(Path(tmp) / "missing"))


class EncryptedBackupTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.main = Path(self.tmp.name) / "backups"
        for override in (
            override_settings(BACKUP_DIR=str(self.main), BACKUP_COPY_DIR="", BACKUP_PASSPHRASE=PASS),
        ):
            override.enable()
            self.addCleanup(override.disable)

    def make(self, chunks=GOOD):
        with mock.patch("clinic.backup._stream_dump", dump_of(chunks)):
            return backup.make_backup("scheduled", now=NOW)

    def test_a_backup_is_encrypted_checked_and_recorded_as_such(self):
        result = self.make()
        self.assertTrue(result.ok, result.error)
        self.assertTrue(result.encrypted)
        self.assertTrue(result.path.name.endswith(".sql.gz.enc"))
        with self.assertRaises(gzip.BadGzipFile):
            gzip.open(result.path).read()  # not readable without the passphrase
        backup.verify_dump(result.path)
        status = backup.backup_status(NOW)
        self.assertEqual((status["state"], status["encryption_on"], status["last_encrypted"]), ("ok", True, True))

    def test_the_passphrase_is_needed_to_verify(self):
        result = self.make()
        with override_settings(BACKUP_PASSPHRASE=""):
            with self.assertRaisesMessage(backup.BackupError, "BACKUP_PASSPHRASE"):
                backup.verify_dump(result.path)
        with override_settings(BACKUP_PASSPHRASE="wrong"):
            with self.assertRaisesMessage(backup.BackupError, "Wrong passphrase"):
                backup.verify_dump(result.path)

    def test_a_cut_off_encrypted_backup_does_not_verify(self):
        result = self.make()
        result.path.write_bytes(result.path.read_bytes()[:-20])
        with self.assertRaises(backup.BackupError):
            backup.verify_dump(result.path)

    def test_encrypted_backups_are_listed_and_rotated_like_the_rest(self):
        self.make()
        self.assertEqual([p.name for p in backup.list_backups()], ["mediflow-20261124-120000-scheduled.sql.gz.enc"])
        old = self.main / "mediflow-20261101-120000-scheduled.sql.gz.enc"
        old.write_bytes(encrypt(SQL))
        removed = backup.rotate(self.main, NOW, 7)
        self.assertEqual(removed, [old.name])

    def test_a_restore_reads_through_the_encryption(self):
        result = self.make()
        sent = []
        process = mock.Mock()
        process.stdin.write.side_effect = sent.append
        process.stderr.read.return_value = b""
        process.wait.return_value = 0
        with mock.patch("subprocess.Popen", return_value=process):
            backup._load_dump(result.path)
        self.assertIn(b"CREATE TABLE", b"".join(sent))

    def test_with_no_passphrase_backups_stay_plain_as_before(self):
        with override_settings(BACKUP_PASSPHRASE=""):
            result = self.make()
        self.assertFalse(result.encrypted)
        self.assertTrue(result.path.name.endswith(".sql.gz"))
        self.assertEqual(gzip.open(result.path).read(), SQL)


S3 = dict(
    BACKUP_S3_ENDPOINT="http://127.0.0.1:1", BACKUP_S3_BUCKET="clinic-backups", BACKUP_S3_ACCESS_KEY="AKIDTEST",
    BACKUP_S3_SECRET_KEY="secret/key+test", BACKUP_S3_REGION="eu-west-1", BACKUP_S3_PREFIX="mediflow", NODE_CLINIC_TIN="TIN9",
)


class SigningTests(SimpleTestCase):
    """AWS publishes worked examples, so the signing code is checked against them, not against itself."""

    SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"

    def test_the_published_signing_key_example(self):
        key = offsite.signing_key(self.SECRET, "20120215", "us-east-1", "iam")
        self.assertEqual(key.hex(), "f4780e2d9f65fa895f9c67b32ce1baf0b0d8a43505a000a1a9e090d414db404d")

    def test_the_published_get_vanilla_request(self):
        headers = offsite.sign(
            "GET", "https://example.amazonaws.com/", region="us-east-1", service="service",
            access_key="AKIDEXAMPLE", secret_key=self.SECRET, payload_hash=None,
            now=dt.datetime(2015, 8, 30, 12, 36, 0, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(
            headers["Authorization"],
            "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/service/aws4_request, "
            "SignedHeaders=host;x-amz-date, "
            "Signature=5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31",
        )


class _FakeS3(BaseHTTPRequestHandler):
    status = 200
    received = {}

    def do_PUT(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        stamp = dt.datetime.strptime(self.headers["x-amz-date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=dt.timezone.utc)
        expected = offsite.sign(
            "PUT", f"http://{self.headers['Host']}{self.path}", region="eu-west-1", service="s3",
            access_key="AKIDTEST", secret_key="secret/key+test",
            payload_hash=self.headers["x-amz-content-sha256"], now=stamp,
        )["Authorization"]
        type(self).received = {
            "path": self.path, "body": body, "signed_ok": self.headers["Authorization"] == expected,
            "hash_ok": self.headers["x-amz-content-sha256"] == hashlib.sha256(body).hexdigest(),
        }
        self.send_response(type(self).status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class UploadTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _FakeS3.status, _FakeS3.received = 200, {}
        self.server = HTTPServer(("127.0.0.1", 0), _FakeS3)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        override = override_settings(**{**S3, "BACKUP_S3_ENDPOINT": f"http://127.0.0.1:{self.server.server_address[1]}"})
        override.enable()
        self.addCleanup(override.disable)

    def encrypted_file(self) -> Path:
        path = Path(self.tmp.name) / "mediflow-20261124-120000-scheduled.sql.gz.enc"
        path.write_bytes(encrypt(SQL))
        return path

    def test_an_encrypted_backup_is_uploaded_signed_and_intact(self):
        path = self.encrypted_file()
        key = offsite.upload(path)
        self.assertEqual(key, "mediflow/TIN9/mediflow-20261124-120000-scheduled.sql.gz.enc")
        got = _FakeS3.received
        self.assertEqual(got["path"], f"/clinic-backups/{key}")
        self.assertEqual(got["body"], path.read_bytes())
        self.assertTrue(got["signed_ok"], "the signature does not match the request that was sent")
        self.assertTrue(got["hash_ok"])

    def test_a_plain_backup_is_never_uploaded(self):
        plain = Path(self.tmp.name) / "mediflow-20261124-120000-scheduled.sql.gz"
        with gzip.open(plain, "wb") as handle:
            handle.write(SQL)
        with self.assertRaisesMessage(offsite.OffsiteError, "unencrypted"):
            offsite.upload(plain)
        self.assertEqual(_FakeS3.received, {})

    def test_a_refusal_from_the_service_is_reported(self):
        _FakeS3.status = 403
        with self.assertRaisesMessage(offsite.OffsiteError, "403"):
            offsite.upload(self.encrypted_file())

    def test_no_connection_is_reported_plainly(self):
        with override_settings(BACKUP_S3_ENDPOINT="http://127.0.0.1:1"):
            with self.assertRaisesMessage(offsite.OffsiteError, "could not be reached"):
                offsite.upload(self.encrypted_file(), timeout=3)

    def test_not_set_up_is_an_error_not_a_crash(self):
        with override_settings(BACKUP_S3_BUCKET=""):
            self.assertFalse(offsite.configured())
            with self.assertRaisesMessage(offsite.OffsiteError, "not set up"):
                offsite.upload(self.encrypted_file())


class OffsiteInBackupTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        override = override_settings(BACKUP_DIR=str(Path(self.tmp.name) / "b"), BACKUP_COPY_DIR="", **S3)
        override.enable()
        self.addCleanup(override.disable)

    def make(self, passphrase):
        with override_settings(BACKUP_PASSPHRASE=passphrase):
            with mock.patch("clinic.backup._stream_dump", dump_of(GOOD)):
                return backup.make_backup("scheduled", now=NOW)

    def test_an_encrypted_backup_is_sent_off_site(self):
        with mock.patch("clinic.offsite.upload", return_value="k") as upload:
            result = self.make(PASS)
        upload.assert_called_once()
        self.assertEqual((result.ok, result.offsite_ok), (True, True))
        status = backup.backup_status(NOW)
        self.assertEqual((status["offsite_configured"], status["offsite_ok"]), (True, True))
        self.assertIsNotNone(status["last_offsite_at"])

    def test_without_encryption_nothing_leaves_the_clinic(self):
        with mock.patch("clinic.offsite.upload") as upload:
            result = self.make("")
        upload.assert_not_called()
        self.assertTrue(result.ok)  # the local backup is fine
        self.assertFalse(result.offsite_ok)
        self.assertIn("BACKUP_PASSPHRASE", result.offsite_error)

    def test_a_failed_upload_does_not_fail_the_backup(self):
        with mock.patch("clinic.offsite.upload", side_effect=offsite.OffsiteError("could not be reached")):
            result = self.make(PASS)
        self.assertEqual((result.ok, result.offsite_ok), (True, False))
        status = backup.backup_status(NOW)
        self.assertEqual(status["state"], "ok")
        self.assertIn("could not be reached", status["offsite_error"])
