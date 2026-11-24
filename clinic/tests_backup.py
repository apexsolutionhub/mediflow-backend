"""Offline-sync phase 4: local backups, their status, rotation and restore."""
import gzip
import tempfile
from datetime import datetime, timedelta
from datetime import timezone as tz
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, TestCase, override_settings

from clinic import backup
from clinic.models import SyncState

GOOD = [
    b"-- MySQL dump 10.13  Distrib 8.0\n",
    b"CREATE TABLE `clinic_patient` (`id` int);\n",
    b"INSERT INTO `clinic_patient` VALUES (1);\n",
    b"-- Dump completed on 2026-11-24 09:00:00\n",
]
NOW = datetime(2026, 11, 24, 12, 0, 0, tzinfo=tz.utc)


def dump_of(chunks):
    def fake(_command, _env):
        yield from chunks

    return fake


def put_dump(folder: Path, when: datetime, reason="scheduled", chunks=GOOD):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / backup._name(when, reason)
    with gzip.open(path, "wb") as handle:
        handle.write(b"".join(chunks))
    return path


class BackupCase(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.main = Path(self.tmp.name) / "backups"
        self.second = Path(self.tmp.name) / "usb"
        override = override_settings(BACKUP_DIR=str(self.main), BACKUP_COPY_DIR="")
        override.enable()
        self.addCleanup(override.disable)

    def run_backup(self, chunks=GOOD, reason="manual", now=NOW):
        with mock.patch("clinic.backup._stream_dump", dump_of(chunks)):
            return backup.make_backup(reason, now=now)


class MakeBackupTests(BackupCase):
    def test_a_good_dump_is_saved_checked_and_recorded(self):
        result = self.run_backup()
        self.assertTrue(result.ok, result.error)
        self.assertEqual(result.path.name, "mediflow-20261124-120000-manual.sql.gz")
        self.assertEqual(list(self.main.glob("*.part")), [])
        backup.verify_dump(result.path)
        status = backup.backup_status(NOW)
        self.assertEqual((status["state"], status["count"]), ("ok", 1))
        self.assertEqual(status["last_backup_at"], NOW.isoformat())

    def test_the_reason_is_kept_safe_in_the_file_name(self):
        result = self.run_backup(reason="pre sync/../x")
        self.assertRegex(result.path.name, r"^mediflow-\d{8}-\d{6}-presyncx\.sql\.gz$")

    def test_a_failed_dump_leaves_nothing_behind_and_shows_as_failed(self):
        def broken(_c, _e):
            yield b"-- MySQL dump\n"
            raise backup.BackupError("mysqldump failed (2): access denied")

        with mock.patch("clinic.backup._stream_dump", broken):
            result = backup.make_backup("manual", now=NOW)
        self.assertFalse(result.ok)
        self.assertIn("access denied", result.error)
        self.assertEqual(list(self.main.glob("mediflow-*")), [])
        status = backup.backup_status(NOW)
        self.assertEqual(status["state"], "failed")
        self.assertIn("access denied", status["last_error"])

    def test_an_incomplete_dump_is_rejected(self):
        result = self.run_backup(chunks=GOOD[:3])  # no "Dump completed" line
        self.assertFalse(result.ok)
        self.assertIn("incomplete", result.error)
        self.assertEqual(list(self.main.glob("mediflow-*")), [])

    def test_an_empty_dump_is_rejected(self):
        result = self.run_backup(chunks=[])
        self.assertFalse(result.ok)
        self.assertIn("empty", result.error)

    def test_a_damaged_file_does_not_verify(self):
        path = put_dump(self.main, NOW)
        path.write_bytes(path.read_bytes()[:-12])
        with self.assertRaises(backup.BackupError):
            backup.verify_dump(path)

    def test_a_failed_backup_never_removes_the_good_ones(self):
        good = self.run_backup(now=NOW - timedelta(hours=5))
        self.run_backup(chunks=[], now=NOW)
        self.assertTrue(good.path.exists())
        self.assertEqual(backup.backup_status(NOW)["state"], "failed")


class SecondLocationTests(BackupCase):
    def test_each_backup_is_copied_to_the_second_location(self):
        with override_settings(BACKUP_COPY_DIR=str(self.second)):
            result = self.run_backup()
            status = backup.backup_status(NOW)
        self.assertTrue(result.secondary_ok)
        self.assertTrue((self.second / result.path.name).is_file())
        self.assertEqual((status["secondary_configured"], status["secondary_ok"]), (True, True))

    def test_an_unplugged_drive_is_a_warning_not_a_failed_backup(self):
        blocker = Path(self.tmp.name) / "not-a-folder"
        blocker.write_text("x")
        with override_settings(BACKUP_COPY_DIR=str(blocker / "usb")):
            result = self.run_backup()
            status = backup.backup_status(NOW)
        self.assertTrue(result.ok)
        self.assertFalse(result.secondary_ok)
        self.assertEqual(status["state"], "ok")
        self.assertFalse(status["secondary_ok"])
        self.assertIn("Second location", status["secondary_error"])

    def test_no_second_location_is_not_an_error(self):
        status = (self.run_backup(), backup.backup_status(NOW))[1]
        self.assertFalse(status["secondary_configured"])
        self.assertIsNone(status["secondary_ok"])


class RotationTests(BackupCase):
    def test_recent_dumps_are_all_kept_then_one_per_day_for_a_week(self):
        for hours in (1, 5, 9, 13, 20):  # today: every one stays
            put_dump(self.main, NOW - timedelta(hours=hours))
        for hours in (30, 32, 34):  # yesterday (the 23rd): only the newest stays
            put_dump(self.main, NOW - timedelta(hours=hours))
        put_dump(self.main, NOW - timedelta(days=3, hours=1))  # an older day: kept
        put_dump(self.main, NOW - timedelta(days=9))  # past a week: removed
        removed = backup.rotate(self.main, NOW, 7)
        kept = backup.list_backups(self.main)
        self.assertEqual(len(kept), 5 + 1 + 1)
        self.assertEqual(len(removed), 3)
        self.assertNotIn(backup._name(NOW - timedelta(days=9), "scheduled"), [p.name for p in kept])

    def test_rotation_ignores_files_that_are_not_backups(self):
        (self.main).mkdir(parents=True)
        (self.main / "notes.txt").write_text("keep me")
        backup.rotate(self.main, NOW, 7)
        self.assertTrue((self.main / "notes.txt").exists())

    def test_making_a_backup_prunes_old_ones(self):
        put_dump(self.main, NOW - timedelta(days=12))
        result = self.run_backup()
        self.assertEqual(len(result.removed), 1)


class StatusTests(BackupCase):
    def test_no_backup_yet_is_missing(self):
        status = backup.backup_status(NOW)
        self.assertEqual((status["state"], status["count"], status["last_backup_at"]), ("missing", 0, None))

    def test_an_old_backup_is_stale(self):
        self.run_backup(now=NOW - timedelta(hours=40))
        self.assertEqual(backup.backup_status(NOW)["state"], "stale")
        self.assertEqual(backup.backup_status(NOW - timedelta(hours=10))["state"], "ok")

    def test_a_success_after_a_failure_is_ok_again(self):
        self.run_backup(chunks=[], now=NOW - timedelta(hours=2))
        self.assertEqual(backup.backup_status(NOW)["state"], "failed")
        self.run_backup(now=NOW)
        self.assertEqual(backup.backup_status(NOW)["state"], "ok")

    def test_history_is_newest_first_and_capped(self):
        for n in range(25):
            self.run_backup(now=NOW + timedelta(minutes=n))
        status = backup._read_status()
        self.assertEqual(len(status["history"]), backup.MAX_HISTORY)
        self.assertEqual(status["history"][0]["at"], (NOW + timedelta(minutes=24)).isoformat())


class DumpCommandTests(SimpleTestCase):
    def test_the_password_is_never_on_the_command_line(self):
        databases = {"default": {"NAME": "clinic", "USER": "mediflow", "PASSWORD": "s3cret!", "HOST": "127.0.0.1", "PORT": "3306"}}
        with override_settings(DATABASES=databases):
            command, env = backup.dump_command()
        self.assertNotIn("s3cret!", " ".join(command))
        self.assertEqual(env["MYSQL_PWD"], "s3cret!")
        self.assertIn("--single-transaction", command)
        self.assertEqual(command[-1], "clinic")

    def test_a_missing_mysqldump_says_how_to_fix_it(self):
        with mock.patch("subprocess.Popen", side_effect=FileNotFoundError):
            with self.assertRaisesMessage(backup.BackupError, "MYSQLDUMP_PATH"):
                list(backup._stream_dump(["mysqldump"], {}))


class RunBackupCommandTests(BackupCase):
    def test_success_prints_the_file(self):
        out = StringIO()
        with mock.patch("clinic.backup._stream_dump", dump_of(GOOD)):
            call_command("run_backup", "--reason", "scheduled", stdout=out)
        self.assertIn("Backup saved", out.getvalue())
        self.assertEqual(len(backup.list_backups()), 1)

    def test_failure_is_an_error_so_the_scheduler_sees_it(self):
        with mock.patch("clinic.backup._stream_dump", dump_of([])):
            with self.assertRaises(CommandError):
                call_command("run_backup", stdout=StringIO())


class RestoreTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.main = Path(self.tmp.name) / "backups"
        override = override_settings(BACKUP_DIR=str(self.main), NODE_CLINIC_TIN="R-TIN", NODE_BRANCH="Main")
        override.enable()
        self.addCleanup(override.disable)

    def restore(self, *args):
        out = StringIO()
        call_command("restore_backup", *args, stdout=out)
        return out.getvalue()

    def test_it_will_not_run_without_confirmation(self):
        put_dump(self.main, NOW)
        with mock.patch("clinic.backup._load_dump") as load:
            with self.assertRaisesMessage(CommandError, "--yes"):
                self.restore()
        load.assert_not_called()

    def test_it_restores_the_newest_backup_and_requires_a_sync(self):
        put_dump(self.main, NOW - timedelta(hours=8))
        newest = put_dump(self.main, NOW)
        with mock.patch("clinic.backup._load_dump") as load:
            out = self.restore("--yes")
        load.assert_called_once_with(newest)
        self.assertIn("run a sync", out)
        self.assertTrue(SyncState.objects.get(clinic_tin="R-TIN", branch_name="Main").restore_pending)

    def test_a_damaged_backup_is_never_loaded(self):
        path = put_dump(self.main, NOW)
        path.write_bytes(b"not a gzip")
        with mock.patch("clinic.backup._load_dump") as load:
            with self.assertRaises(CommandError):
                self.restore("--yes", "--file", str(path))
        load.assert_not_called()
        self.assertFalse(SyncState.objects.filter(clinic_tin="R-TIN").exists())

    def test_no_backups_is_a_clear_error(self):
        with self.assertRaisesMessage(CommandError, "No backups found"):
            self.restore("--yes")


class PresyncBackupTests(TestCase):
    def run_row(self, kind="sync"):
        from clinic.models import SyncRun

        return SyncRun.objects.create(clinic_tin="P-TIN", branch_name="Main", kind=kind)

    def hook(self, run, db="default", result=None, **settings_over):
        from clinic.sync_client import _presync_backup

        with override_settings(BACKUP_BEFORE_SYNC=True, **settings_over):
            with mock.patch("clinic.backup.make_backup", return_value=result) as make:
                _presync_backup(run, db)
        return make

    def test_a_backup_is_taken_and_noted_on_the_run(self):
        run = self.run_row()
        ok = backup.BackupResult(ok=True, path=Path("mediflow-20261124-120000-presync.sql.gz"))
        make = self.hook(run, result=ok)
        make.assert_called_once_with("presync")
        run.refresh_from_db()
        self.assertEqual(run.receipt["backup"], {"ok": True, "file": "mediflow-20261124-120000-presync.sql.gz", "error": ""})

    def test_a_failed_backup_is_noted_and_does_not_stop_the_sync(self):
        run = self.run_row()
        self.hook(run, result=backup.BackupResult(ok=False, error="mysqldump was not found."))
        run.refresh_from_db()
        self.assertEqual((run.receipt["backup"]["ok"], run.receipt["backup"]["error"]), (False, "mysqldump was not found."))

    def test_it_is_skipped_for_a_seed_for_another_database_and_when_switched_off(self):
        self.hook(self.run_row("seed"), result=None).assert_not_called()
        self.hook(self.run_row(), db="node", result=None).assert_not_called()
        run = self.run_row()
        from clinic.sync_client import _presync_backup

        with override_settings(BACKUP_BEFORE_SYNC=False):
            with mock.patch("clinic.backup.make_backup") as make:
                _presync_backup(run, "default")
        make.assert_not_called()


class BackupApiTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        from api.models import UserProfile

        self.manager = get_user_model().objects.create_user("bkmgr", password="x")
        UserProfile.objects.create(user=self.manager, clinic_tin="B-TIN", role="manager", branch_name="Main")
        self.nurse = get_user_model().objects.create_user("bknurse", password="x")
        UserProfile.objects.create(user=self.nurse, clinic_tin="B-TIN", role="nurse", branch_name="Main")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def as_user(self, user):
        from rest_framework.test import APIClient

        client = APIClient()
        client.force_authenticate(user)
        return client

    @override_settings(NODE_MODE="clinic")
    def test_status_includes_the_backup_block_on_a_clinic_server(self):
        with override_settings(BACKUP_DIR=str(Path(self.tmp.name) / "b")):
            res = self.as_user(self.manager).get("/api/sync/status/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["backup"]["state"], "missing")

    @override_settings(NODE_MODE="cloud")
    def test_the_cloud_has_no_backup_block(self):
        self.assertIsNone(self.as_user(self.manager).get("/api/sync/status/").json()["backup"])

    @override_settings(NODE_MODE="clinic")
    def test_back_up_now_works_for_the_manager(self):
        ok = backup.BackupResult(ok=True, path=Path("x.sql.gz"))
        with mock.patch("clinic.backup.make_backup", return_value=ok) as make:
            res = self.as_user(self.manager).post("/api/sync/backup/")
        self.assertEqual(res.status_code, 200)
        make.assert_called_once_with("manual")
        self.assertIn("state", res.json()["backup"])

    @override_settings(NODE_MODE="clinic")
    def test_a_failed_backup_says_why(self):
        bad = backup.BackupResult(ok=False, error="mysqldump failed (2): access denied")
        with mock.patch("clinic.backup.make_backup", return_value=bad):
            res = self.as_user(self.manager).post("/api/sync/backup/")
        self.assertEqual(res.status_code, 502)
        self.assertIn("access denied", res.json()["detail"])

    @override_settings(NODE_MODE="clinic")
    def test_staff_cannot_back_up(self):
        with mock.patch("clinic.backup.make_backup") as make:
            res = self.as_user(self.nurse).post("/api/sync/backup/")
        self.assertEqual(res.status_code, 403)
        make.assert_not_called()

    @override_settings(NODE_MODE="cloud")
    def test_the_cloud_refuses(self):
        with mock.patch("clinic.backup.make_backup") as make:
            res = self.as_user(self.manager).post("/api/sync/backup/")
        self.assertEqual(res.status_code, 400)
        make.assert_not_called()


class RealToolBehaviourTests(SimpleTestCase):
    """Things the first real mysqldump / mysql drill showed."""

    DATABASES = {"default": {"NAME": "clinic", "USER": "u", "PASSWORD": "p", "HOST": "127.0.0.1", "PORT": "3306"}}

    def command(self, supports):
        backup._supports.cache_clear()
        with override_settings(DATABASES=self.DATABASES):
            with mock.patch("clinic.backup._supports", return_value=supports):
                return backup.dump_command()[0]

    def test_the_gtid_line_is_left_out_when_the_tool_supports_it(self):
        command = self.command(True)
        self.assertIn("--set-gtid-purged=OFF", command)
        self.assertEqual(command[-1], "clinic")  # the database name stays last

    def test_a_tool_without_the_option_is_not_given_it(self):
        self.assertNotIn("--set-gtid-purged=OFF", self.command(False))

    def test_the_option_check_reads_the_tools_help(self):
        backup._supports.cache_clear()
        fake = mock.Mock(stdout=b"  --set-gtid-purged[=name]\n", stderr=b"")
        with mock.patch("subprocess.run", return_value=fake):
            self.assertTrue(backup._supports("mysqldump-a", "set-gtid-purged"))
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout=b"--other\n", stderr=b"")):
            self.assertFalse(backup._supports("mysqldump-b", "set-gtid-purged"))
        with mock.patch("subprocess.run", side_effect=FileNotFoundError):
            self.assertFalse(backup._supports("mysqldump-c", "set-gtid-purged"))

    def test_restore_skips_the_gtid_line_but_loads_everything_else(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "x.sql.gz"
        with gzip.open(path, "wb") as handle:
            handle.write(b"-- MySQL dump\nSET @@GLOBAL.GTID_PURGED=/*!80000 '+'*/ 'abc:1-9';\nCREATE TABLE `a` (`id` int);\n")
        sent = []
        process = mock.Mock()
        process.stdin.write.side_effect = sent.append
        process.stderr.read.return_value = b""
        process.wait.return_value = 0
        with mock.patch("subprocess.Popen", return_value=process):
            backup._load_dump(path)
        loaded = b"".join(sent)
        self.assertNotIn(b"GTID_PURGED", loaded)
        self.assertIn(b"CREATE TABLE", loaded)
