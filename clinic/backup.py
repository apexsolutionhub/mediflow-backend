"""Local database backups for a clinic server (docs/OFFLINE_SYNC_CONTRACT.md, section 11).

Sync is not backup: until a day's work reaches the cloud, the clinic server's database is the only
copy. This module makes a compressed dump, checks it, copies it to a second disk or USB drive when
one is set, keeps the last week, and records the outcome so the manager can see it.

Settings (all optional):
  BACKUP_DIR          where dumps go (default: a `backups` folder next to the backend)
  BACKUP_COPY_DIR     a second disk or USB drive to copy each dump to
  BACKUP_KEEP_DAYS    how many days of dumps to keep (default 7)
  BACKUP_STALE_HOURS  a backup older than this shows as "stale" (default 36)
  BACKUP_PASSPHRASE   encrypt every backup with this passphrase (kept with the manager: no passphrase, no restore)
  BACKUP_S3_*         off-site copy of encrypted backups to S3-compatible storage (see clinic/offsite.py)
  MYSQLDUMP_PATH      the mysqldump program (default: found on PATH)
"""
import functools
import gzip
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from . import backup_crypto, offsite

PREFIX = "mediflow-"
SUFFIX = ".sql.gz"
ENCRYPTED_SUFFIX = ".sql.gz.enc"
STATUS_FILE = "backup_status.json"
CHUNK = 1024 * 256
MAX_HISTORY = 20


class BackupError(Exception):
    """A backup that could not be made or did not check out."""


@dataclass
class BackupResult:
    ok: bool
    path: Path | None = None
    size: int = 0
    reason: str = ""
    error: str = ""
    secondary_ok: bool | None = None  # None: no second location is set
    secondary_error: str = ""
    encrypted: bool = False
    offsite_ok: bool | None = None  # None: off-site storage is not set up
    offsite_error: str = ""
    removed: list = field(default_factory=list)


# ---------- settings ----------
def backup_dir() -> Path:
    configured = getattr(settings, "BACKUP_DIR", "")
    return Path(configured) if configured else Path(settings.BASE_DIR).parent / "backups"


def copy_dir() -> Path | None:
    configured = getattr(settings, "BACKUP_COPY_DIR", "")
    return Path(configured) if configured else None


def keep_days() -> int:
    return max(1, int(getattr(settings, "BACKUP_KEEP_DAYS", 7) or 7))


def passphrase() -> str:
    return str(getattr(settings, "BACKUP_PASSPHRASE", "") or "")


def stale_hours() -> int:
    return max(1, int(getattr(settings, "BACKUP_STALE_HOURS", 36) or 36))


# ---------- the dump ----------
@functools.lru_cache(maxsize=8)
def _supports(program: str, option: str) -> bool:
    """Whether this mysqldump knows an option (MariaDB's, for one, has no --set-gtid-purged)."""
    try:
        result = subprocess.run([program, "--help"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return option.encode() in result.stdout + result.stderr


def dump_command() -> tuple[list, dict]:
    """The mysqldump command and its environment. The password goes in the environment, never on the command line."""
    db = settings.DATABASES["default"]
    program = getattr(settings, "MYSQLDUMP_PATH", "") or shutil.which("mysqldump") or "mysqldump"
    command = [
        program,
        f"--host={db.get('HOST') or '127.0.0.1'}",
        f"--port={db.get('PORT') or 3306}",
        f"--user={db.get('USER') or 'root'}",
        "--single-transaction",
        "--routines",
        "--triggers",
        "--no-tablespaces",
        "--default-character-set=utf8mb4",
    ]
    # A server that uses GTIDs makes mysqldump write "SET @@GLOBAL.GTID_PURGED", which needs SUPER to load back.
    if _supports(program, "set-gtid-purged"):
        command.append("--set-gtid-purged=OFF")
    command.append(str(db["NAME"]))
    env = {**os.environ, "MYSQL_PWD": str(db.get("PASSWORD") or "")}
    return command, env


def _stream_dump(command, env):
    """Yield the dump in chunks; raise BackupError if mysqldump is missing or fails."""
    try:
        process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise BackupError("mysqldump was not found. Install MySQL's command-line tools or set MYSQLDUMP_PATH.") from exc
    try:
        while chunk := process.stdout.read(CHUNK):
            yield chunk
    finally:
        process.stdout.close()
        stderr = process.stderr.read().decode("utf-8", "replace").strip()
        process.stderr.close()
        code = process.wait()
    if code != 0:
        raise BackupError(f"mysqldump failed ({code}): {stderr[:500] or 'no message'}")


def open_dump(path: Path):
    """A readable stream of the dump's SQL (decrypting and unzipping), whether or not the file is encrypted."""
    try:
        if backup_crypto.is_encrypted(path):
            reader = backup_crypto.DecryptingReader(open(path, "rb"), passphrase())
            return gzip.GzipFile(fileobj=reader, mode="rb")
        return gzip.open(path, "rb")
    except backup_crypto.CryptoError as exc:
        raise BackupError(str(exc)) from exc


def verify_dump(path: Path):
    """A readable gzip that starts and ends like a finished mysqldump."""
    head, tail, total = b"", b"", 0
    try:
        with open_dump(path) as handle:
            while chunk := handle.read(CHUNK):
                if not head:
                    head = chunk[:4096]
                tail = (tail + chunk)[-4096:]
                total += len(chunk)
    except backup_crypto.CryptoError as exc:
        raise BackupError(str(exc)) from exc
    except (OSError, EOFError) as exc:
        raise BackupError(f"The backup file is damaged ({exc}).") from exc
    if total == 0 or (b"MySQL dump" not in head and b"CREATE TABLE" not in head + tail):
        raise BackupError("The backup file is empty or is not a database dump.")
    if b"Dump completed" not in tail:
        raise BackupError("The backup file is incomplete (the dump did not finish).")


def _name(now: datetime, reason: str, encrypted: bool = False) -> str:
    safe = "".join(c for c in reason if c.isalnum() or c in "-_")[:20] or "manual"
    stamp = now.astimezone(dt_timezone.utc).strftime("%Y%m%d-%H%M%S")  # names are always UTC
    return f"{PREFIX}{stamp}-{safe}{ENCRYPTED_SUFFIX if encrypted else SUFFIX}"


def parse_time(path: Path) -> datetime | None:
    """When a dump was taken, read from its file name."""
    try:
        stamp = path.name[len(PREFIX) : len(PREFIX) + 15]
        return datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=dt_timezone.utc)
    except ValueError:
        return None


def list_backups(folder: Path | None = None) -> list:
    folder = folder or backup_dir()
    if not folder.is_dir():
        return []
    files = [p for p in folder.glob(f"{PREFIX}*") if p.name.endswith((SUFFIX, ENCRYPTED_SUFFIX)) and parse_time(p)]
    return sorted(files, key=lambda p: p.name, reverse=True)


def rotate(folder: Path, now: datetime, days: int) -> list:
    """Keep every dump from the last 24 hours, then the newest of each earlier day up to `days` days."""
    removed = []
    keep, seen_days = set(), set()
    for path in list_backups(folder):
        taken = parse_time(path)
        age = now - taken
        if age <= timedelta(hours=24):
            keep.add(path)
        elif age <= timedelta(days=days) and taken.date() not in seen_days:
            seen_days.add(taken.date())
            keep.add(path)
    for path in list_backups(folder):
        if path not in keep:
            try:
                path.unlink()
                removed.append(path.name)
            except OSError:
                pass
    return removed


def _write_dump(path: Path, chunks, encrypt: bool):
    """Write the dump to `path`: gzip, and AES-encrypted on top when a passphrase is set."""
    with open(path, "wb") as raw:
        if encrypt:
            writer = backup_crypto.EncryptingWriter(raw, passphrase())
            try:
                with gzip.GzipFile(fileobj=writer, mode="wb") as out:
                    for chunk in chunks:
                        out.write(chunk)
            finally:
                writer.close()
        else:
            with gzip.GzipFile(fileobj=raw, mode="wb") as out:
                for chunk in chunks:
                    out.write(chunk)


def make_backup(reason: str = "manual", now: datetime | None = None) -> BackupResult:
    """Dump, check, copy to the second location, rotate, and record the outcome. Never raises."""
    now = now or timezone.now()
    folder = backup_dir()
    result = BackupResult(ok=False, reason=reason)
    temp = None
    try:
        folder.mkdir(parents=True, exist_ok=True)
        encrypt = bool(passphrase())
        final = folder / _name(now, reason, encrypted=encrypt)
        temp = final.with_suffix(final.suffix + ".part")
        command, env = dump_command()
        _write_dump(temp, _stream_dump(command, env), encrypt)
        verify_dump(temp)
        result.encrypted = encrypt
        temp.replace(final)
        temp = None
        result.ok, result.path, result.size = True, final, final.stat().st_size
    except (BackupError, backup_crypto.CryptoError, OSError) as exc:
        result.error = str(exc)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)

    if result.ok:
        second = copy_dir()
        if second is not None:
            try:
                second.mkdir(parents=True, exist_ok=True)
                shutil.copy2(result.path, second / result.path.name)
                result.secondary_ok = True
                rotate(second, now, keep_days())
            except OSError as exc:
                result.secondary_ok, result.secondary_error = False, f"Second location unavailable: {exc}"
        result.removed = rotate(folder, now, keep_days())
        if offsite.configured():
            if not result.encrypted:
                result.offsite_ok = False
                result.offsite_error = "Off-site copy needs encrypted backups. Set BACKUP_PASSPHRASE; nothing was uploaded."
            else:
                try:
                    offsite.upload(result.path, now)
                    result.offsite_ok = True
                except offsite.OffsiteError as exc:
                    result.offsite_ok, result.offsite_error = False, str(exc)
    record(result, now)
    return result


# ---------- status ----------
def _status_path() -> Path:
    return backup_dir() / STATUS_FILE


def _read_status() -> dict:
    try:
        return json.loads(_status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def record(result: BackupResult, now: datetime):
    """Remember the outcome next to the backups (it survives restarts, and works without the database)."""
    status = _read_status()
    entry = {
        "at": now.isoformat(),
        "reason": result.reason,
        "ok": result.ok,
        "file": result.path.name if result.path else "",
        "size": result.size,
        "error": result.error,
        "secondary_ok": result.secondary_ok,
        "secondary_error": result.secondary_error,
        "encrypted": result.encrypted,
        "offsite_ok": result.offsite_ok,
        "offsite_error": result.offsite_error,
    }
    status["last_attempt"] = entry
    if result.ok:
        status["last_success"] = entry
    if result.secondary_ok:
        status["last_secondary_success"] = entry
    if result.offsite_ok:
        status["last_offsite_success"] = entry
    status["history"] = ([entry] + status.get("history", []))[:MAX_HISTORY]
    try:
        backup_dir().mkdir(parents=True, exist_ok=True)
        temp = _status_path().with_suffix(".tmp")
        temp.write_text(json.dumps(status, indent=2), encoding="utf-8")
        temp.replace(_status_path())
    except OSError:
        pass  # the status is a convenience; the backup files are the real thing


def backup_status(now: datetime | None = None) -> dict:
    """What the manager sees: "ok", "stale" (too old), "failed" (the last try failed) or "missing" (none yet)."""
    now = now or timezone.now()
    status = _read_status()
    last_ok, last_try = status.get("last_success"), status.get("last_attempt")
    files = list_backups()
    state = "missing"
    if last_ok:
        age = now - datetime.fromisoformat(last_ok["at"])
        state = "stale" if age > timedelta(hours=stale_hours()) else "ok"
        if last_try and not last_try["ok"] and datetime.fromisoformat(last_try["at"]) > datetime.fromisoformat(last_ok["at"]):
            state = "failed"
    elif last_try and not last_try["ok"]:
        state = "failed"
    second = copy_dir()
    return {
        "state": state,
        "last_backup_at": last_ok["at"] if last_ok else None,
        "last_size": last_ok["size"] if last_ok else None,
        "last_error": last_try["error"] if last_try and not last_try["ok"] else "",
        "count": len(files),
        "secondary_configured": second is not None,
        "secondary_ok": (last_ok or {}).get("secondary_ok") if second is not None else None,
        "secondary_error": (last_try or {}).get("secondary_error", "") if second is not None else "",
        "last_secondary_at": (status.get("last_secondary_success") or {}).get("at"),
        # Tier 2: whether new backups are encrypted, and whether an off-site copy is set up and working.
        "encryption_on": bool(passphrase()),
        "last_encrypted": bool((last_ok or {}).get("encrypted")),
        "offsite_configured": offsite.configured(),
        "offsite_ok": (last_ok or {}).get("offsite_ok") if offsite.configured() else None,
        "offsite_error": (last_try or {}).get("offsite_error", "") if offsite.configured() else "",
        "last_offsite_at": (status.get("last_offsite_success") or {}).get("at"),
        "history": status.get("history", [])[:5],
    }


# ---------- restore ----------
def _load_dump(path: Path):
    """Feed a dump to the mysql client. Replaces the contents of the clinic database."""
    db = settings.DATABASES["default"]
    program = getattr(settings, "MYSQL_PATH", "") or shutil.which("mysql") or "mysql"
    command = [
        program,
        f"--host={db.get('HOST') or '127.0.0.1'}",
        f"--port={db.get('PORT') or 3306}",
        f"--user={db.get('USER') or 'root'}",
        "--default-character-set=utf8mb4",
        str(db["NAME"]),
    ]
    env = {**os.environ, "MYSQL_PWD": str(db.get("PASSWORD") or "")}
    try:
        process = subprocess.Popen(command, env=env, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise BackupError("The mysql program was not found. Install MySQL's command-line tools or set MYSQL_PATH.") from exc
    with open_dump(path) as handle:
        try:
            for line in handle:
                # Older dumps from a GTID server carry this line; it needs SUPER and is not data.
                if line.startswith(b"SET @@GLOBAL.GTID_PURGED"):
                    continue
                process.stdin.write(line)
        except BrokenPipeError:
            pass
    process.stdin.close()
    stderr = process.stderr.read().decode("utf-8", "replace").strip()
    process.stderr.close()
    if process.wait() != 0:
        raise BackupError(f"Loading the backup failed: {stderr[:500] or 'no message'}")


def restore_backup(path: Path, scope=None):
    """Load a backup over this server's database, then require a sync before anyone works.

    `scope` is (clinic_tin, branch_name); the sync-required flag is set after the restore because the
    restored tables are the ones from the moment of the backup.
    """
    verify_dump(path)
    _load_dump(path)
    if scope:
        from .models import SyncState

        state, _ = SyncState.objects.get_or_create(clinic_tin=scope[0], branch_name=scope[1])
        state.restore_pending = True
        state.save(update_fields=["restore_pending", "updated_at"])
