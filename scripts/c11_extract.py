"""Verified private extraction of a closed PocketBase backup ZIP (C11 full mode).

Run from the repository root, never from the destination:

    python -m scripts.c11_extract --archive X.zip --destination NEW_DIR \\
        --manifest-out NEW_FILE [--expect-manifest private-manifest.json]

The archive is inventoried first with ``scripts.c11_import.inspect_archive``
and stricter member checks (among them at most 32 path components per member);
a refusal creates nothing. Members are then streamed in bounded chunks into a
destination created by this invocation (directories 0700, files 0600). Content
is never executed or interpreted, and links, special files, archive modes and
overwrites are never produced. Each CRC-32 is verified by a complete read,
every file is re-read from disk, and the archive must keep its identity, size,
mtime and SHA-256 from start to end. Open descriptors grow with the depth of
the tree, never with its width: the soft RLIMIT_NOFILE must be at least the
deepest member's component count + 16, checked before extracting.

The manifest has the private format ``{archive_sha256, files: [{path, bytes,
sha256, crc32}]}`` (paths sorted, lowercase hex, two-space JSON). It is written
only after all members were verified, to a temporary file that is then
hard-linked to --manifest-out: that name appears complete or not at all. With
``--expect-manifest`` (a regular file of at most 64 MiB) it must match exactly.
Any failure removes only the destination created by this invocation. Standard
output carries one aggregated JSON summary, never member names. No PostgreSQL,
PocketBase, network or production access.

Exit status:
  0      extracted and verified, manifest written ("ok": true).
  1      refused or failed; nothing is left, except the manifest when only the
         verified content differs from --expect-manifest ("expected_manifest"
         in the summary), kept for a private diff.
  3      the destination created by this run could not be removed (see
         "cleanup_error"): never use it; inspect it, remove it by hand and
         rerun with new paths. Takes precedence over the other failure codes.
  128+N  interrupted by signal N (130 SIGINT, 143 SIGTERM, 129 SIGHUP); the
         destination was removed (see "destination_removed").
A run killed without a summary (SIGKILL, power cut) may leave a partial
destination and a hidden ".c11-extract-*.partial" file next to the manifest:
remove both by hand and rerun with new paths.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import resource
import secrets
import shutil
import signal
import stat
import sys
import time
import zipfile
import zlib
from contextlib import contextmanager
from pathlib import Path

from scripts.c11_import import ImportBlocked, inspect_archive

CHUNK_BYTES = 1 << 20
FREE_SPACE_RESERVE_BYTES = 1 << 30
# The private manifest of the real backup (1 290 entries) is about 0.35 MB.
MANIFEST_MAX_BYTES = 64 << 20
# PocketBase v0.22.34 stores files at storage/<collection>/<record>/<file> and
# thumbnails one level lower (core/base.go:39,530, models/record.go:460-461,
# apis/file.go:158,175): at most 5 components. The bound keeps the descriptors of
# the verification walk and the recursion of shutil.rmtree small for any archive.
MAX_MEMBER_DEPTH = 32
# Descriptors besides one per directory level of the verification walk or of the
# cleanup: standard streams, archive, destination, file, listing, interpreter.
# Measured need: depth + 6 (10 for a depth-4 tree of 1 100 record directories).
DESCRIPTOR_RESERVE = 16
DIRECTORY_MODE = 0o700
FILE_MODE = 0o600
# PocketBase v0.22.34 tools/archive/create.go:53 skips directories and :76 writes
# Deflate members only (same in v0.22.27:76 and v0.40.6:74). STORED is the only
# other method for which zipfile bounds the output of each read.
ALLOWED_METHODS = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
# Traditional (bit 0) and strong (bit 6) encryption, masked local headers (bit 13).
ENCRYPTION_FLAGS = 0x0001 | 0x0040 | 0x2000
SHA256_HEX = re.compile(r"[0-9a-f]{64}")
CRC32_HEX = re.compile(r"[0-9a-f]{8}")
MANIFEST_KEYS = frozenset({"archive_sha256", "files"})
ENTRY_KEYS = frozenset({"path", "bytes", "sha256", "crc32"})
DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
NEW_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
PROGRESS_SECONDS = 10.0
INTERRUPT_SIGNALS = tuple(
    getattr(signal, name) for name in ("SIGINT", "SIGTERM", "SIGHUP") if hasattr(signal, name)
)


class ExtractionFailed(ImportBlocked):
    """A refusal or failed check. ``details`` holds aggregates, never names."""

    def __init__(self, code: str, **details):
        super().__init__(code)
        self.code = code
        self.details = details


def file_identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def member_parts(info: zipfile.ZipInfo) -> tuple[str, ...]:
    name = info.filename[:-1] if info.is_dir() else info.filename
    return tuple(name.split("/"))


def member_blockers(infos: list[zipfile.ZipInfo]) -> list[str]:
    """Checks stricter than inspect_archive. A PocketBase backup triggers none."""
    blockers: set[str] = set()
    files: set[tuple[str, ...]] = set()
    directories: set[tuple[str, ...]] = set()
    parents: set[tuple[str, ...]] = set()
    for info in infos:
        name, parts = info.orig_filename, member_parts(info)
        # zipfile silently truncates a name at NUL: orig_filename keeps the raw one.
        if (
            name != info.filename
            or name.startswith("/")
            or any(part in {"", ".", ".."} for part in parts)
            or any(char in "\\:" or ord(char) < 32 or ord(char) == 127 for char in name)
        ):
            blockers.add("unsafe_archive_member")
        if len(parts) > MAX_MEMBER_DEPTH:
            blockers.add("archive_member_too_deep")
        kind = stat.S_IFMT(info.external_attr >> 16)
        if kind == stat.S_IFLNK:
            blockers.add("symlink_in_archive")
        elif kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
            blockers.add("special_file_in_archive")
        elif kind and (kind == stat.S_IFDIR) != info.is_dir():
            blockers.add("inconsistent_member_type")
        if info.flag_bits & ENCRYPTION_FLAGS:
            blockers.add("encrypted_archive_member")
        if info.compress_type not in ALLOWED_METHODS:
            blockers.add("unsupported_compression_method")
        if info.is_dir():
            # Never read; an empty Deflate stream still has a 2-byte compressed body.
            if info.file_size:
                blockers.add("invalid_directory_member")
            directories.add(parts)
        else:
            files.add(parts)
        parents.update(parts[:depth] for depth in range(1, len(parts)))
    if files & (directories | parents):
        blockers.add("member_path_conflict")
    return sorted(blockers)


def _reject_constant(_value):
    raise ExtractionFailed("invalid_expected_manifest")


def _unique_object(pairs):
    if len({key for key, _ in pairs}) != len(pairs):
        raise ExtractionFailed("invalid_expected_manifest")
    return dict(pairs)


def valid_manifest(value) -> bool:
    if not isinstance(value, dict) or set(value) != MANIFEST_KEYS:
        return False
    checksum, files = value["archive_sha256"], value["files"]
    if not isinstance(checksum, str) or not SHA256_HEX.fullmatch(checksum):
        return False
    if not isinstance(files, list):
        return False
    paths = set()
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != ENTRY_KEYS:
            return False
        path, size, sha256, crc32 = entry["path"], entry["bytes"], entry["sha256"], entry["crc32"]
        if not isinstance(path, str) or not path or path in paths:
            return False
        if type(size) is not int or size < 0:
            return False
        if not isinstance(sha256, str) or not SHA256_HEX.fullmatch(sha256):
            return False
        if not isinstance(crc32, str) or not CRC32_HEX.fullmatch(crc32):
            return False
        paths.add(path)
    return True


def _check_manifest_file(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode):
        raise ExtractionFailed("expected_manifest_not_regular_file")
    if info.st_size > MANIFEST_MAX_BYTES:
        raise ExtractionFailed("expected_manifest_too_large", max_bytes=MANIFEST_MAX_BYTES)


def _read_manifest_bytes(fd: int, limit: int) -> bytes:
    """Read a whole descriptor in chunks, refusing more than ``limit`` bytes."""
    chunks, size = [], 0
    while chunk := os.read(fd, min(CHUNK_BYTES, limit + 1 - size)):
        size += len(chunk)
        if size > limit:
            raise ExtractionFailed("expected_manifest_too_large", max_bytes=limit)
        chunks.append(chunk)
    return b"".join(chunks)


def load_manifest(path: Path) -> tuple[dict, bytes]:
    """Strict private-manifest reader: exact keys, lowercase hex, unique paths.

    Only a regular file of at most MANIFEST_MAX_BYTES is read, with bounded memory.
    It is checked by stat before opening, so a FIFO or device is never opened, and
    again on the descriptor, opened non-blocking, in case the path was swapped.
    """
    _check_manifest_file(os.stat(path))
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        _check_manifest_file(os.fstat(fd))
        raw = _read_manifest_bytes(fd, MANIFEST_MAX_BYTES)
    finally:
        os.close(fd)
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_unique_object, parse_constant=_reject_constant
        )
    except ValueError as exc:
        raise ExtractionFailed("invalid_expected_manifest") from exc
    if not valid_manifest(value):
        raise ExtractionFailed("invalid_expected_manifest")
    return value, raw


def render_manifest(manifest: dict) -> bytes:
    """Same JSON text as write_json_new and as the private manifest, always UTF-8."""
    return (json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()


def compare_manifests(actual: dict, expected: dict, *, with_sha256: bool = True) -> dict:
    """Aggregated exact comparison; never returns a path."""
    found = {entry["path"]: entry for entry in actual["files"]}
    wanted = {entry["path"]: entry for entry in expected["files"]}
    common = found.keys() & wanted.keys()
    keys = ("bytes", "crc32", "sha256") if with_sha256 else ("bytes", "crc32")
    result = {
        "archive_sha256_matches": actual["archive_sha256"] == expected["archive_sha256"],
        "missing_paths": len(wanted.keys() - found.keys()),
        "unexpected_paths": len(found.keys() - wanted.keys()),
    }
    for key in keys:
        result[f"{key}_mismatches"] = sum(found[path][key] != wanted[path][key] for path in common)
    result["matches"] = result["archive_sha256_matches"] and not any(
        value for name, value in result.items() if name != "archive_sha256_matches"
    )
    return result


def _python_path_entries() -> set[Path]:
    entries = set()
    for entry in sys.path:
        try:
            entries.add(Path(entry or os.getcwd()).resolve())
        except (OSError, RuntimeError):
            continue
    return entries


def _check_new_paths(destination: Path, manifest_out: Path) -> None:
    for label, path in (("destination", destination), ("manifest_out", manifest_out)):
        if path.name in {"", ".", ".."}:
            raise ExtractionFailed(f"invalid_{label}")
        if os.path.lexists(path):
            raise ExtractionFailed(f"{label}_exists")
        if not path.parent.is_dir():
            raise ExtractionFailed(f"{label}_parent_missing")
    target = destination.parent.resolve() / destination.name
    output = manifest_out.parent.resolve() / manifest_out.name
    if output == target or target in output.parents:
        raise ExtractionFailed("manifest_out_inside_destination")
    python_path = _python_path_entries()
    # A package extracted next to sys.path entries could be imported by name.
    if target in python_path or target.parent in python_path:
        raise ExtractionFailed("destination_on_python_path")


def _check_platform() -> None:
    needed = (os.open, os.mkdir, os.stat)
    if not all(call in os.supports_dir_fd for call in needed) or os.scandir not in os.supports_fd:
        raise ExtractionFailed("platform_without_dir_fd_support")
    if os.link not in os.supports_follow_symlinks:
        raise ExtractionFailed("platform_without_link_nofollow_support")
    if not hasattr(signal, "pthread_sigmask"):
        raise ExtractionFailed("platform_without_signal_mask_support")


def _check_descriptor_limit(depth: int) -> None:
    """Refuse before extracting when verification or cleanup could run out of descriptors."""
    soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    required = depth + DESCRIPTOR_RESERVE
    if soft != resource.RLIM_INFINITY and soft < required:
        raise ExtractionFailed("descriptor_limit_too_low", soft_limit=soft, required=required)


@contextmanager
def _deferred_signals():
    """Hold SIGINT, SIGTERM and SIGHUP until the block has recorded what it created.

    A signal received meanwhile stays pending. CPython runs its handler as soon as
    the previous mask is restored, from that pthread_sigmask call, so whatever the
    block created is already known to the cleanup.
    """
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, INTERRUPT_SIGNALS)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _unlink_own(path: Path, own: os.stat_result) -> None:
    """Remove ``path`` only while it is still the file this invocation created."""
    try:
        current = os.lstat(path)
    except FileNotFoundError:
        return
    if (current.st_dev, current.st_ino) == (own.st_dev, own.st_ino):
        os.unlink(path)


def _publish_new_private(path: Path, data: bytes, published) -> None:
    """Create ``path`` as a complete 0600 file, or not at all; never overwrite.

    The bytes are written and fsynced to a new temporary file of the same
    directory, which is hard-linked to ``path`` (link() never replaces an existing
    name) and then unlinked. ``published`` runs once ``path`` is in place, before
    a pending signal is handled. A failure never leaves a partial ``path``.
    """
    temporary = path.parent / f".c11-extract-{os.getpid()}-{secrets.token_hex(8)}.partial"
    fd = own = None
    try:
        with _deferred_signals():
            fd = os.open(temporary, NEW_FILE_FLAGS, FILE_MODE)
            own = os.fstat(fd)
        os.fchmod(fd, FILE_MODE)
        _write_all(fd, data)
        os.fsync(fd)
        with _deferred_signals():
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError as exc:
                raise ExtractionFailed("manifest_out_exists") from exc
            linked = os.lstat(path)
            if (linked.st_dev, linked.st_ino) != (own.st_dev, own.st_ino):
                raise ExtractionFailed("manifest_out_replaced")
            published()
    finally:
        try:
            if own is not None:
                _unlink_own(temporary, own)
        finally:
            if fd is not None:
                os.close(fd)


def _open_directories(root_fd: int, parts: tuple[str, ...], created: set) -> int:
    """Open (creating 0700 when absent) a directory below root without following links."""
    fd = os.dup(root_fd)
    try:
        for depth, part in enumerate(parts, start=1):
            try:
                os.mkdir(part, DIRECTORY_MODE, dir_fd=fd)
                made = True
            except FileExistsError:
                made = False
            child = os.open(part, DIRECTORY_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
            if made:
                os.fchmod(fd, DIRECTORY_MODE)
                created.add(parts[:depth])
            elif parts[:depth] not in created:
                raise ExtractionFailed("unexpected_destination_entry")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _extract_member(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, parent_fd: int, leaf: str, chunk_bytes: int
) -> dict:
    """Stream one member into a new 0600 file; never more than one chunk in memory."""
    sha256, crc32, size = hashlib.sha256(), 0, 0
    try:
        fd = os.open(leaf, NEW_FILE_FLAGS, FILE_MODE, dir_fd=parent_fd)
    except FileExistsError as exc:
        raise ExtractionFailed("destination_entry_exists") from exc
    try:
        os.fchmod(fd, FILE_MODE)
        with archive.open(info) as source:
            while chunk := source.read(chunk_bytes):
                sha256.update(chunk)
                crc32 = zlib.crc32(chunk, crc32)
                size += len(chunk)
                _write_all(fd, chunk)
        os.fsync(fd)
    finally:
        os.close(fd)
    if size != info.file_size:
        raise ExtractionFailed("member_size_mismatch")
    if crc32 != info.CRC:
        raise ExtractionFailed("member_crc_mismatch")
    return {
        "path": info.filename,
        "bytes": size,
        "sha256": sha256.hexdigest(),
        "crc32": f"{crc32:08x}",
    }


def _hash_file(dir_fd: int, name: str, expected: os.stat_result, chunk_bytes: int) -> tuple:
    fd = os.open(name, READ_FLAGS, dir_fd=dir_fd)
    try:
        current = os.fstat(fd)
        if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
            raise ExtractionFailed("destination_changed_during_verification")
        sha256, crc32, size = hashlib.sha256(), 0, 0
        while chunk := os.read(fd, chunk_bytes):
            sha256.update(chunk)
            crc32 = zlib.crc32(chunk, crc32)
            size += len(chunk)
    finally:
        os.close(fd)
    return (size, sha256.hexdigest(), f"{crc32:08x}")


def _listed_directory(parent_fd: int, name: str | None, expected: os.stat_result | None):
    """Open a directory (``name`` None: a copy of ``parent_fd``) and list it sorted.

    The descriptor is closed again if the identity check or the listing fails.
    """
    if name is None:
        fd = os.dup(parent_fd)
    else:
        fd = os.open(name, DIRECTORY_FLAGS, dir_fd=parent_fd)
    try:
        if expected is not None:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
                raise ExtractionFailed("destination_changed_during_verification")
        with os.scandir(fd) as scan:
            names = sorted(entry.name for entry in scan)
        return fd, iter(names)
    except BaseException:
        os.close(fd)
        raise


def _verify_tree(root_fd: int, entries: list[dict], directories: set, chunk_bytes: int) -> None:
    """Re-read the destination from disk: exact tree, modes, single links, contents.

    Depth first, keeping open only the directories of the current path: the open
    descriptors grow with the depth of the tree (at most MAX_MEMBER_DEPTH), never
    with the number of sibling directories.
    """
    if stat.S_IMODE(os.fstat(root_fd).st_mode) != DIRECTORY_MODE:
        raise ExtractionFailed("destination_mode_mismatch")
    seen_files, seen_directories = {}, set()
    walk = []  # (relative parts, open descriptor, names left), one per open directory
    try:
        walk.append(((), *_listed_directory(root_fd, None, None)))
        while walk:
            parts, fd, names = walk[-1]
            name = next(names, None)
            if name is None:
                walk.pop()
                os.close(fd)
                continue
            info = os.stat(name, dir_fd=fd, follow_symlinks=False)
            relative = (*parts, name)
            if stat.S_ISLNK(info.st_mode):
                raise ExtractionFailed("symlink_in_destination")
            if stat.S_ISDIR(info.st_mode):
                if stat.S_IMODE(info.st_mode) != DIRECTORY_MODE:
                    raise ExtractionFailed("destination_mode_mismatch")
                walk.append((relative, *_listed_directory(fd, name, info)))
                seen_directories.add(relative)
            elif stat.S_ISREG(info.st_mode):
                if stat.S_IMODE(info.st_mode) != FILE_MODE:
                    raise ExtractionFailed("destination_mode_mismatch")
                if info.st_nlink != 1:
                    raise ExtractionFailed("hardlink_in_destination")
                seen_files["/".join(relative)] = _hash_file(fd, name, info, chunk_bytes)
            else:
                raise ExtractionFailed("special_file_in_destination")
    finally:
        for _, fd, _ in walk:
            os.close(fd)
    wanted = {entry["path"]: (entry["bytes"], entry["sha256"], entry["crc32"]) for entry in entries}
    problems = {
        "missing_files": len(wanted.keys() - seen_files.keys()),
        "unexpected_files": len(seen_files.keys() - wanted.keys()),
        "content_mismatches": sum(
            seen_files[path] != wanted[path] for path in wanted.keys() & seen_files.keys()
        ),
        "missing_directories": len(directories - seen_directories),
        "unexpected_directories": len(seen_directories - directories),
    }
    if any(problems.values()):
        raise ExtractionFailed("destination_verification_failed", **problems)


def _remove_created(destination: Path, identity: tuple[int, int] | None) -> bool:
    """Remove only the directory created by this invocation, never a replacement."""
    try:
        current = os.lstat(destination)
    except FileNotFoundError:
        return True
    if identity is None:
        # Created but never opened: only an empty directory may be removed.
        os.rmdir(destination)
        return True
    if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != identity:
        return False
    shutil.rmtree(destination)
    return not os.path.lexists(destination)


def _failure_code(exc: BaseException) -> str:
    if isinstance(exc, zipfile.BadZipFile):
        message = str(exc)
        if message.startswith("Bad CRC-32"):
            return "member_crc_mismatch"
        if message.startswith("Overlapped entries"):
            return "overlapping_archive_members"
        return "bad_zip_archive"
    if isinstance(exc, zlib.error):
        return "corrupt_compressed_member"
    if isinstance(exc, EOFError):
        return "truncated_archive_member"
    if isinstance(exc, NotImplementedError):
        return "unsupported_zip_feature"
    if isinstance(exc, OSError):
        return "os_error_" + errno.errorcode.get(exc.errno or 0, "unknown").lower()
    return "unexpected_" + type(exc).__name__


def _peak_rss_bytes() -> int:
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak if sys.platform == "darwin" else peak * 1024


def new_summary(archive, destination, manifest_out, expect_manifest=None) -> dict:
    return {
        "scope": "offline_backup_extraction_only",
        "ok": False,
        "phase": "preflight",
        "error": None,
        "details": {},
        "production_authorized": False,
        "postgresql_imported": False,
        "content_interpreted": False,
        "archive": str(archive),
        "destination": str(destination),
        "manifest_out": str(manifest_out),
        "expect_manifest": None if expect_manifest is None else str(expect_manifest),
        "destination_created": False,
        "destination_removed": False,
        "manifest_written": False,
        "timings_seconds": {},
    }


def extract(
    archive: Path,
    destination: Path,
    manifest_out: Path,
    *,
    expect_manifest: Path | None = None,
    summary: dict | None = None,
    chunk_bytes: int = CHUNK_BYTES,
    reserve_bytes: int = FREE_SPACE_RESERVE_BYTES,
    progress=None,
) -> dict:
    """Extract and verify; return the aggregated summary or raise ExtractionFailed.

    ``summary`` (optional) is filled in place, so a caller interrupted by a
    signal still knows whether the destination was created and removed.
    """
    if summary is None:
        summary = new_summary(archive, destination, manifest_out, expect_manifest)
    if chunk_bytes <= 0:
        raise ValueError("chunk_bytes must be positive")
    started = clock = time.monotonic()
    timings = summary["timings_seconds"]

    def lap(name: str) -> None:
        nonlocal clock
        now = time.monotonic()
        timings[name] = round(now - clock, 3)
        clock = now

    created, identity, root_fd = False, None, None
    try:
        _check_platform()
        _check_new_paths(destination, manifest_out)
        expected = expected_raw = None
        if expect_manifest is not None:
            expected, expected_raw = load_manifest(expect_manifest)
        if not stat.S_ISREG(os.stat(archive).st_mode):
            raise ExtractionFailed("archive_not_regular_file")
        with open(archive, "rb") as handle:
            opened_stat = os.fstat(handle.fileno())
            opened = file_identity(opened_stat)
            if not stat.S_ISREG(opened_stat.st_mode):
                raise ExtractionFailed("archive_not_regular_file")
            inventory = inspect_archive(archive)
            before = inventory["archive_sha256"]
            summary.update(
                archive_bytes=opened[2],
                archive_sha256=before,
                inventory_blockers=inventory["blockers"],
            )
            if file_identity(os.stat(archive)) != opened:
                raise ExtractionFailed("archive_changed_during_inventory")
            with zipfile.ZipFile(handle) as source:
                infos = source.infolist()
                blockers = sorted(set(inventory["blockers"]) | set(member_blockers(infos)))
                if blockers:
                    raise ExtractionFailed("archive_inventory_blocked", blockers=blockers)
                members = [info for info in infos if not info.is_dir()]
                declared_bytes = sum(info.file_size for info in members)
                depth = max((len(member_parts(info)) for info in infos), default=0)
                summary.update(
                    members=len(infos),
                    files=len(members),
                    directory_members=len(infos) - len(members),
                    declared_bytes=declared_bytes,
                    max_member_depth=depth,
                )
                _check_descriptor_limit(depth)
                if expected is not None:
                    declared = {
                        "archive_sha256": before,
                        "files": [
                            {"path": i.filename, "bytes": i.file_size, "crc32": f"{i.CRC:08x}"}
                            for i in members
                        ],
                    }
                    early = compare_manifests(declared, expected, with_sha256=False)
                    if not early["matches"]:
                        raise ExtractionFailed(
                            "expected_manifest_mismatch",
                            stage="declared_before_extraction",
                            **early,
                        )
                free = shutil.disk_usage(destination.parent).free
                summary["free_bytes_before"] = free
                if free < declared_bytes + reserve_bytes:
                    raise ExtractionFailed(
                        "insufficient_free_space",
                        required_bytes=declared_bytes + reserve_bytes,
                        free_bytes=free,
                    )
                lap("preflight")
                summary["phase"] = "extract"
                # A signal is handled only once the new directory is recorded for cleanup.
                with _deferred_signals():
                    try:
                        os.mkdir(destination, DIRECTORY_MODE)
                    except FileExistsError as exc:
                        raise ExtractionFailed("destination_exists") from exc
                    created = summary["destination_created"] = True
                    root_fd = os.open(destination, DIRECTORY_FLAGS)
                    root = os.fstat(root_fd)
                    identity = (root.st_dev, root.st_ino)
                os.fchmod(root_fd, DIRECTORY_MODE)
                if os.listdir(root_fd):
                    raise ExtractionFailed("unexpected_destination_entry")
                entries, directories = [], set()
                done_bytes, last = 0, time.monotonic()
                for index, info in enumerate(infos, start=1):
                    parts = member_parts(info)
                    if info.is_dir():
                        os.close(_open_directories(root_fd, parts, directories))
                        continue
                    parent_fd = _open_directories(root_fd, parts[:-1], directories)
                    try:
                        entries.append(
                            _extract_member(source, info, parent_fd, parts[-1], chunk_bytes)
                        )
                    finally:
                        os.close(parent_fd)
                    done_bytes += info.file_size
                    if progress and time.monotonic() - last >= PROGRESS_SECONDS:
                        last = time.monotonic()
                        progress(
                            {
                                "phase": "extract",
                                "members_done": index,
                                "members_total": len(infos),
                                "bytes_done": done_bytes,
                                "bytes_total": declared_bytes,
                            }
                        )
            lap("extract")
            summary["phase"] = "verify"
            if progress:
                progress({"phase": "verify", "files_total": len(entries)})
            _verify_tree(root_fd, entries, directories, chunk_bytes)
            current = os.lstat(destination)
            if (current.st_dev, current.st_ino) != identity:
                raise ExtractionFailed("destination_replaced")
            lap("verify")
            summary["phase"] = "archive_recheck"
            if progress:
                progress({"phase": "archive_recheck"})
            handle.seek(0)
            after = hashlib.file_digest(handle, "sha256").hexdigest()
            summary["archive_sha256_after"] = after
            unchanged = (
                after == before
                and file_identity(os.fstat(handle.fileno())) == opened
                and file_identity(os.stat(archive)) == opened
            )
            summary["archive_unchanged"] = unchanged
            if not unchanged:
                raise ExtractionFailed("archive_changed_during_extraction")
            lap("archive_recheck")
        summary["phase"] = "manifest"
        manifest = {"archive_sha256": before, "files": sorted(entries, key=lambda e: e["path"])}
        data = render_manifest(manifest)
        manifest_sha256 = hashlib.sha256(data).hexdigest()
        comparison = None
        if expected is not None:
            comparison = compare_manifests(manifest, expected)
            summary["expected_manifest"] = {**comparison, "byte_identical": data == expected_raw}
        _publish_new_private(
            manifest_out,
            data,
            lambda: summary.update(manifest_written=True, manifest_sha256=manifest_sha256),
        )
        if comparison is not None and not comparison["matches"]:
            raise ExtractionFailed(
                "expected_manifest_mismatch", stage="verified_content", **comparison
            )
        lap("manifest")
        summary.update(
            ok=True,
            phase="done",
            bytes=sum(entry["bytes"] for entry in entries),
            directories=len(directories),
            crc_all_files_ok=True,
            sha256_all_files_recorded=True,
            destination_reread_verified=True,
            modes={"destination": "0700", "directories": "0700", "files": "0600"},
            free_bytes_after=shutil.disk_usage(destination.parent).free,
        )
        return summary
    except BaseException as exc:
        if root_fd is not None:
            os.close(root_fd)
            root_fd = None
        if isinstance(exc, ExtractionFailed):
            summary.update(error=exc.code, details=exc.details)
        elif isinstance(exc, KeyboardInterrupt | SystemExit):
            summary["error"] = "interrupted"
        else:
            # Only the type: zipfile messages may contain a member name.
            summary.update(error=_failure_code(exc), details={"exception": type(exc).__name__})
        if created:
            try:
                summary["destination_removed"] = _remove_created(destination, identity)
            except OSError as cleanup:
                summary["cleanup_error"] = _failure_code(cleanup)
            if not summary["destination_removed"]:
                summary.setdefault("cleanup_error", "destination_replaced_or_not_removed")
        if isinstance(exc, ExtractionFailed | KeyboardInterrupt | SystemExit):
            raise
        raise ExtractionFailed(summary["error"], **summary["details"]) from exc
    finally:
        if root_fd is not None:
            os.close(root_fd)
        summary["elapsed_seconds"] = round(time.monotonic() - started, 3)
        summary["peak_rss_bytes"] = _peak_rss_bytes()
        summary["chunk_bytes"] = chunk_bytes


def _print_progress(event: dict) -> None:
    print(json.dumps(event), file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--archive", type=Path, required=True, help="Closed backup ZIP, read only")
    parser.add_argument("--destination", type=Path, required=True, help="New directory (0700)")
    parser.add_argument("--manifest-out", type=Path, required=True, help="New manifest (0600)")
    parser.add_argument("--expect-manifest", type=Path, help="Private manifest to match exactly")
    args = parser.parse_args(argv)
    summary = new_summary(args.archive, args.destination, args.manifest_out, args.expect_manifest)
    signalled = []

    def interrupted(signum, _frame):
        # Raise once, so that a repeated signal cannot interrupt the cleanup.
        if not signalled:
            signalled.append(signum)
            if signum == signal.SIGINT:
                raise KeyboardInterrupt
            raise SystemExit(128 + signum)

    handlers = {}
    for signum in INTERRUPT_SIGNALS:
        handlers[signum] = signal.signal(signum, interrupted)
    try:
        extract(
            args.archive,
            args.destination,
            args.manifest_out,
            expect_manifest=args.expect_manifest,
            summary=summary,
            progress=_print_progress,
        )
        code = 0
    except ExtractionFailed:
        code = 1
    except KeyboardInterrupt:
        code = 130
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    finally:
        for signum, handler in handlers.items():
            signal.signal(signum, handler)
    if summary["destination_created"] and not summary["destination_removed"] and not summary["ok"]:
        code = 3  # The partial destination must be inspected and removed by hand.
    print(json.dumps(summary, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
