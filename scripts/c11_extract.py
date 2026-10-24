"""Verified private extraction of a closed PocketBase backup ZIP (C11 full mode).

Run from the repository root, never from the destination:

    python -m scripts.c11_extract --archive X.zip --destination NEW_DIR \\
        --manifest-out NEW_FILE [--expect-manifest private-manifest.json]

The archive is inventoried first with ``scripts.c11_import.inspect_archive``
and stricter member checks; a refusal creates nothing. Members are then
streamed in bounded chunks into a destination created by this invocation
(directories 0700, files 0600). Content is never executed or interpreted, and
links, special files, archive modes and overwrites are never produced. Each
CRC-32 is verified by a complete read, every file is re-read from disk, and the
archive must keep its identity, size, mtime and SHA-256 from start to end.

The manifest has the private format ``{archive_sha256, files: [{path, bytes,
sha256, crc32}]}`` (paths sorted, lowercase hex, two-space JSON). With
``--expect-manifest`` it must match exactly. Any failure removes only the
destination created by this invocation; the manifest is written only after all
members were verified. Standard output carries aggregates, never member names.
No PostgreSQL, PocketBase, network or production access.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import resource
import shutil
import signal
import stat
import sys
import time
import zipfile
import zlib
from pathlib import Path

from scripts.c11_import import ImportBlocked, inspect_archive

CHUNK_BYTES = 1 << 20
FREE_SPACE_RESERVE_BYTES = 1 << 30
DIRECTORY_MODE = 0o700
FILE_MODE = 0o600
# PocketBase writes Deflate members (tools/archive/create.go). STORED is the
# only other method for which zipfile bounds the output of each read.
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


def load_manifest(path: Path) -> tuple[dict, bytes]:
    """Strict private-manifest reader: exact keys, lowercase hex, unique paths."""
    raw = path.read_bytes()
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


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _write_new_private(path: Path, data: bytes) -> None:
    fd = os.open(path, NEW_FILE_FLAGS, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)
        _write_all(fd, data)
        os.fsync(fd)
    finally:
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


def _verify_tree(root_fd: int, entries: list[dict], directories: set, chunk_bytes: int) -> None:
    """Re-read the destination from disk: exact tree, modes, single links, contents."""
    if stat.S_IMODE(os.fstat(root_fd).st_mode) != DIRECTORY_MODE:
        raise ExtractionFailed("destination_mode_mismatch")
    seen_files, seen_directories = {}, set()
    pending = [((), os.dup(root_fd))]
    try:
        while pending:
            parts, fd = pending.pop()
            try:
                with os.scandir(fd) as scan:
                    names = sorted(entry.name for entry in scan)
                for name in names:
                    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    relative = (*parts, name)
                    if stat.S_ISLNK(info.st_mode):
                        raise ExtractionFailed("symlink_in_destination")
                    if stat.S_ISDIR(info.st_mode):
                        if stat.S_IMODE(info.st_mode) != DIRECTORY_MODE:
                            raise ExtractionFailed("destination_mode_mismatch")
                        child = os.open(name, DIRECTORY_FLAGS, dir_fd=fd)
                        pending.append((relative, child))
                        opened = os.fstat(child)
                        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                            raise ExtractionFailed("destination_changed_during_verification")
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
                os.close(fd)
    finally:
        for _, fd in pending:
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
                summary.update(
                    members=len(infos),
                    files=len(members),
                    directory_members=len(infos) - len(members),
                    declared_bytes=declared_bytes,
                )
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
        comparison = None
        if expected is not None:
            comparison = compare_manifests(manifest, expected)
            summary["expected_manifest"] = {**comparison, "byte_identical": data == expected_raw}
        try:
            _write_new_private(manifest_out, data)
        except FileExistsError as exc:
            raise ExtractionFailed("manifest_out_exists") from exc
        summary.update(manifest_written=True, manifest_sha256=hashlib.sha256(data).hexdigest())
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
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            handlers[name] = signal.signal(getattr(signal, name), interrupted)
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
        for name, handler in handlers.items():
            signal.signal(getattr(signal, name), handler)
    if summary["destination_created"] and not summary["destination_removed"] and not summary["ok"]:
        code = 3  # The partial destination must be inspected and removed by hand.
    print(json.dumps(summary, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
