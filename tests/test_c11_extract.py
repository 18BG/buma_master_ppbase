"""Verified private ZIP extraction for the C11 full mode, on synthetic archives only."""

import errno
import io
import json
import os
import random
import signal
import stat
import struct
import subprocess
import sys
import tracemalloc
import types
import warnings
import zipfile
import zlib
from hashlib import sha256
from pathlib import Path

import pytest

from scripts import c11_extract
from scripts.c11_extract import (
    CHUNK_BYTES,
    ExtractionFailed,
    extract,
    load_manifest,
    main,
    render_manifest,
)
from scripts.c11_import import digest

ROOT = Path(__file__).resolve().parents[1]
DATE = (2026, 9, 15, 23, 18, 12)
FILE = "storage/collection1/record1/picture_abc.png"
REGULAR = stat.S_IFREG | 0o644


def payload(size: int, seed: int) -> bytes:
    return random.Random(seed).randbytes(size)


def backup_members() -> dict[str, bytes]:
    """Same layout as the real backup: 8 root files, empty WALs, storage depth 4 and 5."""
    return {
        "README.md": b"synthetic backup, no real data\n",
        "data.db": b"SQLite format 3\x00" + payload(60_000, 1),
        "data.db-shm": bytes(32_768),
        "data.db-wal": b"",
        "logs.db": bytes(200_000),
        "logs.db-shm": bytes(32_768),
        "logs.db-wal": b"",
        "types.d.ts": b"// synthetic typings\n",
        FILE: payload(70_000, 2),
        FILE + ".attrs": b'{"user.cache_control":""}',
        "storage/collection1/record1/thumbs_picture_abc.png/100x100_picture_abc.png": payload(
            3_000, 3
        ),
        "storage/collection2/record2/empty.txt": b"",
    }


class Unseekable(io.RawIOBase):
    """Forces data descriptors (flag bit 3), as Go's archive/zip writes them."""

    def __init__(self, target):
        self.target = target

    def writable(self):
        return True

    def write(self, data):
        return self.target.write(data)


def write_archive(path, members, *, method=zipfile.ZIP_DEFLATED, modes=None, streamed=False):
    """PocketBase-like ZIP (tools/archive/create.go): Deflate, Unix regular files."""
    with path.open("wb") as target:
        with zipfile.ZipFile(Unseekable(target) if streamed else target, "w") as archive:
            for name, data in members.items():
                info = zipfile.ZipInfo(name, date_time=DATE)
                info.compress_type = method
                info.create_system = 3
                info.external_attr = (modes or {}).get(name, REGULAR) << 16
                archive.writestr(info, data)
    return path


def backup(tmp_path, members=None, **options):
    return write_archive(tmp_path / "backup.zip", members or backup_members(), **options)


def central_headers(raw: bytes):
    end = raw.rindex(b"PK\x05\x06")
    count, offset = (
        struct.unpack_from("<H", raw, end + 10)[0],
        struct.unpack_from("<I", raw, end + 16)[0],
    )
    for _ in range(count):
        assert raw[offset : offset + 4] == b"PK\x01\x02"
        name_length, extra_length, comment_length = struct.unpack_from("<HHH", raw, offset + 28)
        yield offset, raw[offset + 46 : offset + 46 + name_length]
        offset += 46 + name_length + extra_length + comment_length


def patch_central(path, name, field, value):
    position, layout = {"flags": (8, "<H"), "crc": (16, "<I")}[field]
    raw = bytearray(path.read_bytes())
    for offset, entry in list(central_headers(bytes(raw))):
        if entry == name.encode():
            struct.pack_into(layout, raw, offset + position, value)
    path.write_bytes(bytes(raw))


def flip_member_byte(path, name):
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(name)
    raw = bytearray(path.read_bytes())
    name_length, extra_length = struct.unpack_from("<HH", raw, info.header_offset + 26)
    raw[info.header_offset + 30 + name_length + extra_length + info.compress_size // 2] ^= 0xFF
    path.write_bytes(bytes(raw))


def outputs(tmp_path):
    return tmp_path / "out", tmp_path / "manifest.json"


def refused(tmp_path, archive, **options) -> tuple[str, dict, dict]:
    destination, manifest = outputs(tmp_path)
    summary = c11_extract.new_summary(archive, destination, manifest)
    with pytest.raises(ExtractionFailed) as failure:
        extract(archive, destination, manifest, summary=summary, **options)
    assert summary["ok"] is False and summary["error"] == failure.value.code
    return failure.value.code, failure.value.details, summary


def tree_modes(root: Path) -> dict[str, int]:
    modes = {".": stat.S_IMODE(root.lstat().st_mode)}
    for path in root.rglob("*"):
        modes[path.relative_to(root).as_posix()] = path.lstat().st_mode
    return modes


def assert_nothing_created(tmp_path, before):
    assert sorted(p.name for p in tmp_path.iterdir()) == before


@pytest.mark.parametrize("streamed", [False, True])
def test_pocketbase_like_archive_is_extracted_verified_and_private(tmp_path, streamed):
    members = backup_members()
    archive = backup(tmp_path, members, streamed=streamed)
    with zipfile.ZipFile(archive) as source:
        assert all(bool(i.flag_bits & 0x08) is streamed for i in source.infolist())
    checksum = digest(archive)
    destination, manifest_path = outputs(tmp_path)
    summary = extract(archive, destination, manifest_path)
    assert summary["ok"] and summary["phase"] == "done" and summary["error"] is None
    assert summary["archive_unchanged"] and digest(archive) == checksum
    assert summary["archive_sha256"] == summary["archive_sha256_after"] == checksum
    assert (summary["files"], summary["bytes"]) == (12, sum(map(len, members.values())))
    assert summary["crc_all_files_ok"] and summary["destination_reread_verified"]
    assert not summary["production_authorized"] and not summary["postgresql_imported"]
    for name, data in members.items():
        assert (destination / name).read_bytes() == data
    modes = tree_modes(destination)
    assert modes.pop(".") == 0o700
    for name, mode in modes.items():
        expected = (stat.S_IFREG | 0o600) if name in members else (stat.S_IFDIR | 0o700)
        assert mode == expected, name
    text = manifest_path.read_text()
    manifest = json.loads(text)
    # Exactly the private manifest format: key order, sorted paths, lowercase hex.
    assert list(manifest) == ["archive_sha256", "files"]
    assert text == json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    assert manifest["archive_sha256"] == checksum
    assert [entry["path"] for entry in manifest["files"]] == sorted(members)
    for entry in manifest["files"]:
        data = members[entry["path"]]
        assert list(entry) == ["path", "bytes", "sha256", "crc32"]
        assert entry == {
            "path": entry["path"],
            "bytes": len(data),
            "sha256": sha256(data).hexdigest(),
            "crc32": f"{zlib.crc32(data):08x}",
        }
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    assert summary["manifest_sha256"] == sha256(text.encode()).hexdigest()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["backup.zip", "manifest.json", "out"]


def test_expected_manifest_matches_exactly_and_byte_for_byte(tmp_path):
    archive = backup(tmp_path)
    first, manifest = outputs(tmp_path)
    extract(archive, first, manifest)
    second, again = tmp_path / "second", tmp_path / "second.json"
    summary = extract(archive, second, again, expect_manifest=manifest)
    assert summary["ok"] and summary["expected_manifest"] == {
        "archive_sha256_matches": True,
        "missing_paths": 0,
        "unexpected_paths": 0,
        "bytes_mismatches": 0,
        "crc32_mismatches": 0,
        "sha256_mismatches": 0,
        "matches": True,
        "byte_identical": True,
    }
    assert again.read_bytes() == manifest.read_bytes()
    value, raw = load_manifest(manifest)
    assert render_manifest(value) == raw


def mutate(manifest, kind):
    entry = next(e for e in manifest["files"] if e["path"] == FILE)
    if kind == "sha256":
        entry["sha256"] = "0" * 64
    elif kind == "crc32":
        entry["crc32"] = f"{int(entry['crc32'], 16) ^ 1:08x}"
    elif kind == "bytes":
        entry["bytes"] += 1
    elif kind == "missing_entry":
        manifest["files"].remove(entry)
    elif kind == "extra_entry":
        manifest["files"].append({**entry, "path": FILE + ".extra"})
    elif kind == "renamed_entry":
        entry["path"] = FILE + ".renamed"
    elif kind == "archive_sha256":
        manifest["archive_sha256"] = "f" * 64


@pytest.mark.parametrize(
    "kind,stage,counter",
    [
        ("sha256", "verified_content", "sha256_mismatches"),
        ("crc32", "declared_before_extraction", "crc32_mismatches"),
        ("bytes", "declared_before_extraction", "bytes_mismatches"),
        ("missing_entry", "declared_before_extraction", "unexpected_paths"),
        ("extra_entry", "declared_before_extraction", "missing_paths"),
        ("renamed_entry", "declared_before_extraction", "missing_paths"),
        ("archive_sha256", "declared_before_extraction", None),
    ],
)
def test_divergent_expected_manifest_is_refused(tmp_path, kind, stage, counter):
    archive = backup(tmp_path)
    reference = tmp_path / "reference.json"
    extract(archive, tmp_path / "reference", reference)
    manifest = json.loads(reference.read_text())
    mutate(manifest, kind)
    expected = tmp_path / "expected.json"
    expected.write_text(json.dumps(manifest))
    before = sorted(p.name for p in tmp_path.iterdir())
    code, details, summary = refused(tmp_path, archive, expect_manifest=expected)
    assert code == "expected_manifest_mismatch" and details["stage"] == stage
    assert details["matches"] is False
    if counter:
        assert details[counter] == 1
    else:
        assert details["archive_sha256_matches"] is False
    assert not (tmp_path / "out").exists()
    if stage == "verified_content":
        # Every member was verified: the private manifest is kept for a private diff.
        assert summary["destination_created"] and summary["destination_removed"]
        assert summary["manifest_written"] and summary["expected_manifest"]["matches"] is False
        assert json.loads((tmp_path / "manifest.json").read_text())["files"]
    else:
        assert not summary["destination_created"] and not summary["manifest_written"]
        assert_nothing_created(tmp_path, before)


def malformed_manifests() -> list[str]:
    entry = {"path": "x", "bytes": 1, "sha256": "a" * 64, "crc32": "0000000a"}

    def dumped(**top):
        return json.dumps({"archive_sha256": "a" * 64, "files": [entry], **top})

    return [
        dumped(extra=1),
        json.dumps({"archive_sha256": "a" * 64}),
        dumped(archive_sha256="A" * 64),
        dumped(files={}),
        dumped(files=[{key: value for key, value in entry.items() if key != "crc32"}]),
        dumped(files=[{**entry, "extra": 1}]),
        dumped(files=[{**entry, "bytes": "1"}]),
        dumped(files=[{**entry, "bytes": True}]),
        dumped(files=[{**entry, "bytes": -1}]),
        dumped(files=[{**entry, "crc32": "a"}]),
        dumped(files=[{**entry, "crc32": "0000000A"}]),
        dumped(files=[{**entry, "path": ""}]),
        dumped(files=[entry, entry]),
        dumped().replace('"files"', '"archive_sha256": "' + "b" * 64 + '", "files"'),
        dumped().replace('"bytes": 1', '"bytes": NaN'),
        "[]",
        "not json",
    ]


@pytest.mark.parametrize("text", malformed_manifests())
def test_malformed_expected_manifest_is_refused_before_reading_the_archive(tmp_path, text):
    archive = backup(tmp_path)
    expected = tmp_path / "expected.json"
    expected.write_text(text)
    before = sorted(p.name for p in tmp_path.iterdir())
    code, _, summary = refused(tmp_path, archive, expect_manifest=expected)
    assert code == "invalid_expected_manifest" and "archive_sha256" not in summary
    assert_nothing_created(tmp_path, before)


@pytest.mark.parametrize(
    "method,corruption,codes",
    [
        (zipfile.ZIP_STORED, "data", {"member_crc_mismatch"}),
        (zipfile.ZIP_DEFLATED, "data", {"member_crc_mismatch", "corrupt_compressed_member"}),
        (zipfile.ZIP_DEFLATED, "central_crc", {"member_crc_mismatch"}),
    ],
)
def test_corrupted_member_fails_and_removes_the_partial_destination(
    tmp_path, method, corruption, codes
):
    archive = backup(tmp_path, method=method)
    if corruption == "data":
        flip_member_byte(archive, FILE)
    else:
        with zipfile.ZipFile(archive) as source:
            patch_central(archive, FILE, "crc", source.getinfo(FILE).CRC ^ 1)
    checksum = digest(archive)
    (tmp_path / "out-sibling").mkdir()
    (tmp_path / "out-sibling" / "keep.bin").write_bytes(b"keep")
    before = sorted(p.name for p in tmp_path.iterdir())
    code, _, summary = refused(tmp_path, archive)
    assert code in codes and summary["phase"] == "extract"
    assert summary["destination_created"] and summary["destination_removed"]
    assert not summary["manifest_written"]
    assert_nothing_created(tmp_path, before)
    assert (tmp_path / "out-sibling" / "keep.bin").read_bytes() == b"keep"
    assert digest(archive) == checksum


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "storage/../up",
        "/absolute",
        "storage\\windows",
        "c:drive",
        "storage//empty-component",
        "./dot",
        "storage/./dot",
        "storage/control\nchar",
        "storage/nulXname",
    ],
)
def test_unsafe_member_names_are_refused_before_creating_anything(tmp_path, name):
    archive = backup(tmp_path, {**backup_members(), name: b"payload"})
    if "nulX" in name:
        raw = archive.read_bytes()
        assert raw.count(b"nulXname") == 2  # local header and central directory
        archive.write_bytes(raw.replace(b"nulXname", b"nul\x00name"))
        with zipfile.ZipFile(archive) as source:
            assert any(i.orig_filename != i.filename for i in source.infolist())
    before = sorted(p.name for p in tmp_path.iterdir())
    code, details, summary = refused(tmp_path, archive)
    assert code == "archive_inventory_blocked" and "unsafe_archive_member" in details["blockers"]
    assert not summary["destination_created"]
    assert_nothing_created(tmp_path, before)


@pytest.mark.parametrize(
    "mode,blocker",
    [
        (stat.S_IFLNK | 0o777, "symlink_in_archive"),
        (stat.S_IFIFO | 0o644, "special_file_in_archive"),
        (stat.S_IFCHR | 0o644, "special_file_in_archive"),
        (stat.S_IFBLK | 0o644, "special_file_in_archive"),
        (stat.S_IFSOCK | 0o644, "special_file_in_archive"),
        (stat.S_IFDIR | 0o755, "inconsistent_member_type"),
    ],
)
def test_links_and_special_members_are_refused(tmp_path, mode, blocker):
    archive = backup(
        tmp_path,
        {**backup_members(), "storage/c/r/link": b"/etc/passwd"},
        modes={"storage/c/r/link": mode},
    )
    before = sorted(p.name for p in tmp_path.iterdir())
    code, details, _ = refused(tmp_path, archive)
    assert code == "archive_inventory_blocked" and blocker in details["blockers"]
    assert_nothing_created(tmp_path, before)


def duplicate(tmp_path):
    path = backup(tmp_path)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr(FILE, b"second copy")
    return path


def flagged(bit):
    def build(tmp_path):
        path = backup(tmp_path)
        with zipfile.ZipFile(path) as archive:
            patch_central(path, FILE, "flags", archive.getinfo(FILE).flag_bits | bit)
        return path

    return build


@pytest.mark.parametrize(
    "build,blocker",
    [
        (duplicate, "duplicate_archive_member"),
        (flagged(0x0001), "encrypted_archive_member"),
        (flagged(0x0040), "encrypted_archive_member"),
        (lambda p: backup(p, method=zipfile.ZIP_BZIP2), "unsupported_compression_method"),
        (lambda p: backup(p, method=zipfile.ZIP_LZMA), "unsupported_compression_method"),
        (
            lambda p: backup(p, {**backup_members(), "data.db-wal": b"frames"}),
            "live_or_incomplete_sqlite_snapshot",
        ),
        (
            lambda p: backup(p, {k: v for k, v in backup_members().items() if k != "data.db"}),
            "exactly_one_data_db_required",
        ),
        (
            lambda p: backup(p, {**backup_members(), "nested/data.db": b"second"}),
            "exactly_one_data_db_required",
        ),
        (
            lambda p: backup(p, {**backup_members(), FILE + "/inside": b"x"}),
            "member_path_conflict",
        ),
        (
            lambda p: backup(
                p, {**backup_members(), "storage/dir/": b"data"}, modes={"storage/dir/": 0}
            ),
            "invalid_directory_member",
        ),
    ],
)
def test_archive_anomalies_are_refused_before_creating_anything(tmp_path, build, blocker):
    archive = build(tmp_path)
    before = sorted(p.name for p in tmp_path.iterdir())
    code, details, _ = refused(tmp_path, archive)
    assert code == "archive_inventory_blocked" and blocker in details["blockers"]
    assert_nothing_created(tmp_path, before)


@pytest.mark.parametrize("kind", ["directory", "file", "symlink", "dangling_symlink"])
def test_existing_destination_is_never_touched(tmp_path, kind):
    archive = backup(tmp_path)
    destination, _ = outputs(tmp_path)
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "keep.bin").write_bytes(b"keep")
    if kind == "directory":
        destination.mkdir()
        (destination / "keep.bin").write_bytes(b"keep")
    elif kind == "file":
        destination.write_bytes(b"keep")
    elif kind == "symlink":
        destination.symlink_to(tmp_path / "elsewhere")
    else:
        destination.symlink_to(tmp_path / "missing")
    before = {p.name: p.lstat() for p in tmp_path.iterdir()}
    code, _, summary = refused(tmp_path, archive)
    assert code == "destination_exists" and not summary["destination_created"]
    assert {p.name: p.lstat() for p in tmp_path.iterdir()} == before
    assert sorted(p.name for p in (tmp_path / "elsewhere").iterdir()) == ["keep.bin"]
    if kind == "directory":
        assert sorted(p.name for p in destination.iterdir()) == ["keep.bin"]


def test_output_paths_are_checked_before_reading_the_archive(tmp_path, monkeypatch):
    archive = backup(tmp_path)
    destination, manifest = outputs(tmp_path)
    manifest.write_text("previous")
    with pytest.raises(ExtractionFailed, match="manifest_out_exists"):
        extract(archive, destination, manifest)
    assert manifest.read_text() == "previous" and not destination.exists()
    with pytest.raises(ExtractionFailed, match="manifest_out_inside_destination"):
        extract(archive, destination, destination)
    with pytest.raises(ExtractionFailed, match="destination_parent_missing"):
        extract(archive, tmp_path / "missing" / "out", tmp_path / "new.json")
    with pytest.raises(ExtractionFailed, match="manifest_out_parent_missing"):
        extract(archive, destination, tmp_path / "missing" / "new.json")
    monkeypatch.setattr(sys, "path", [str(tmp_path), *sys.path])
    with pytest.raises(ExtractionFailed, match="destination_on_python_path"):
        extract(archive, destination, tmp_path / "new.json")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["backup.zip", "manifest.json"]


@pytest.mark.parametrize("umask", [0o000, 0o022, 0o077])
def test_modes_ignore_archive_permissions_and_umask(tmp_path, umask):
    members = {**backup_members(), "storage/open/": b"", "storage/open/setuid.bin": b"x"}
    modes = {
        "storage/open/": stat.S_IFDIR | 0o777,
        "storage/open/setuid.bin": stat.S_IFREG | 0o4777,
        FILE: stat.S_IFREG | 0o777,
    }
    archive = backup(tmp_path, members, modes=modes)
    with zipfile.ZipFile(archive) as source:
        directory = source.getinfo("storage/open/")
    # An empty Deflate stream still has a compressed body; the entry is never read.
    assert (directory.file_size, directory.compress_size) == (0, 2)
    previous = os.umask(umask)
    try:
        destination, manifest = outputs(tmp_path)
        summary = extract(archive, destination, manifest)
    finally:
        os.umask(previous)
    assert summary["ok"] and summary["directory_members"] == 1
    modes_found = tree_modes(destination)
    assert modes_found.pop(".") == 0o700
    assert set(modes_found.values()) == {stat.S_IFREG | 0o600, stat.S_IFDIR | 0o700}
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    # Directory members create private directories but are not manifest files.
    paths = [entry["path"] for entry in json.loads(manifest.read_text())["files"]]
    assert "storage/open/" not in paths and "storage/open/setuid.bin" in paths


@pytest.mark.parametrize("failure", ["enospc", "interrupt"])
def test_failure_removes_only_the_destination_created_by_this_invocation(
    tmp_path, monkeypatch, failure
):
    archive = backup(tmp_path)
    (tmp_path / "out-sibling").mkdir()
    (tmp_path / "out-sibling" / "keep.bin").write_bytes(b"keep")
    before = sorted(p.name for p in tmp_path.iterdir())
    original, calls = c11_extract._extract_member, []

    def failing(*args):
        calls.append(1)
        if len(calls) == 3:
            if failure == "interrupt":
                raise KeyboardInterrupt
            raise OSError(errno.ENOSPC, "No space left on device")
        return original(*args)

    monkeypatch.setattr(c11_extract, "_extract_member", failing)
    destination, manifest = outputs(tmp_path)
    summary = c11_extract.new_summary(archive, destination, manifest)
    expected = KeyboardInterrupt if failure == "interrupt" else ExtractionFailed
    with pytest.raises(expected):
        extract(archive, destination, manifest, summary=summary)
    assert summary["error"] == ("interrupted" if failure == "interrupt" else "os_error_enospc")
    assert summary["destination_created"] and summary["destination_removed"]
    assert_nothing_created(tmp_path, before)
    assert (tmp_path / "out-sibling" / "keep.bin").read_bytes() == b"keep"


def test_a_replaced_destination_is_never_removed(tmp_path, monkeypatch):
    archive = backup(tmp_path)
    destination, manifest = outputs(tmp_path)
    original = c11_extract._verify_tree

    def replace_after_verification(*args):
        original(*args)
        destination.rename(tmp_path / "moved")
        destination.mkdir()
        (destination / "someone-else.bin").write_bytes(b"not ours")

    monkeypatch.setattr(c11_extract, "_verify_tree", replace_after_verification)
    code, _, summary = refused(tmp_path, archive)
    assert code == "destination_replaced" and summary["destination_removed"] is False
    assert summary["cleanup_error"] == "destination_replaced_or_not_removed"
    assert (destination / "someone-else.bin").read_bytes() == b"not ours"
    assert (tmp_path / "moved" / "data.db").exists() and not manifest.exists()


def test_streaming_reads_bounded_chunks_and_never_holds_a_whole_member(tmp_path, monkeypatch):
    big = 32 * CHUNK_BYTES
    members = {
        **backup_members(),
        "logs.db": bytes(big),  # about 32 KiB once deflated: a 1000:1 expansion
        FILE: payload(big, 4),  # incompressible
    }
    archive = backup(tmp_path, members)
    sizes, original = [], zipfile.ZipExtFile.read

    def spy(self, n=-1):
        sizes.append(n)
        return original(self, n)

    monkeypatch.setattr(zipfile.ZipExtFile, "read", spy)
    destination, manifest = outputs(tmp_path)
    tracemalloc.start()
    try:
        summary = extract(archive, destination, manifest)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert summary["ok"] and summary["bytes"] > 2 * big
    assert sizes and set(sizes) == {CHUNK_BYTES}
    assert peak < 8 * CHUNK_BYTES, peak  # a whole member would need 32 MiB
    assert (destination / "logs.db").stat().st_size == big


@pytest.mark.parametrize("change", ["append", "mtime", "replace"])
def test_archive_change_during_extraction_is_detected(tmp_path, monkeypatch, change):
    archive = backup(tmp_path)
    original, calls = c11_extract._extract_member, []

    def changing(*args):
        result = original(*args)
        if not calls:
            calls.append(1)
            if change == "append":
                with archive.open("ab") as stream:
                    stream.write(b"tail")
            elif change == "mtime":
                info = archive.stat()
                os.utime(archive, ns=(info.st_atime_ns, info.st_mtime_ns + 10**9))
            else:
                copy = tmp_path / "copy.zip"
                copy.write_bytes(archive.read_bytes())
                os.utime(copy, ns=(archive.stat().st_atime_ns, archive.stat().st_mtime_ns))
                copy.replace(archive)
        return result

    monkeypatch.setattr(c11_extract, "_extract_member", changing)
    code, _, summary = refused(tmp_path, archive)
    assert code == "archive_changed_during_extraction" and summary["archive_unchanged"] is False
    assert summary["destination_removed"] and not summary["manifest_written"]
    assert not (tmp_path / "out").exists() and not (tmp_path / "manifest.json").exists()


def tamper(destination: Path, kind: str) -> None:
    if kind == "content":
        fd = os.open(destination / "data.db", os.O_WRONLY)
        os.write(fd, b"X")
        os.close(fd)
    elif kind == "removed":
        (destination / "README.md").unlink()
    elif kind == "extra_file":
        fd = os.open(destination / "extra.bin", os.O_CREAT | os.O_WRONLY, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
    elif kind == "extra_directory":
        (destination / "extra").mkdir()
        (destination / "extra").chmod(0o700)
    elif kind == "mode":
        (destination / "README.md").chmod(0o644)
    elif kind == "hardlink":
        os.link(destination / "README.md", destination / "README.md.link")
    elif kind == "symlink":
        (destination / "link").symlink_to("/etc/hostname")
    elif kind == "fifo":
        os.mkfifo(destination / "fifo", 0o600)


@pytest.mark.parametrize(
    "kind,code,counter",
    [
        ("content", "destination_verification_failed", "content_mismatches"),
        ("removed", "destination_verification_failed", "missing_files"),
        ("extra_file", "destination_verification_failed", "unexpected_files"),
        ("extra_directory", "destination_verification_failed", "unexpected_directories"),
        ("mode", "destination_mode_mismatch", None),
        ("hardlink", "hardlink_in_destination", None),
        ("symlink", "symlink_in_destination", None),
        ("fifo", "special_file_in_destination", None),
    ],
)
def test_destination_is_reread_and_tampering_is_detected(
    tmp_path, monkeypatch, kind, code, counter
):
    archive = backup(tmp_path)
    original = c11_extract._verify_tree

    def tampered(*args):
        tamper(tmp_path / "out", kind)
        return original(*args)

    monkeypatch.setattr(c11_extract, "_verify_tree", tampered)
    found, details, summary = refused(tmp_path, archive)
    assert found == code and summary["phase"] == "verify"
    if counter:
        assert details[counter] == 1
    assert summary["destination_removed"] and not (tmp_path / "out").exists()
    assert not summary["manifest_written"]


def test_cleanup_helper_refuses_a_directory_it_did_not_create(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    (other / "keep.bin").write_bytes(b"keep")
    identity = (other.stat().st_dev, other.stat().st_ino + 1)
    assert c11_extract._remove_created(other, identity) is False
    assert (other / "keep.bin").read_bytes() == b"keep"


def test_insufficient_free_space_is_refused_before_creating_anything(tmp_path, monkeypatch):
    archive = backup(tmp_path)
    monkeypatch.setattr(
        c11_extract.shutil, "disk_usage", lambda _: types.SimpleNamespace(free=4096)
    )
    before = sorted(p.name for p in tmp_path.iterdir())
    code, details, _ = refused(tmp_path, archive)
    assert code == "insufficient_free_space" and details["free_bytes"] == 4096
    assert_nothing_created(tmp_path, before)


@pytest.mark.parametrize("kind", ["not_a_zip", "truncated", "directory", "fifo"])
def test_unreadable_or_irregular_archive_is_refused_without_blocking(tmp_path, kind):
    archive = tmp_path / "backup.zip"
    if kind == "not_a_zip":
        archive.write_bytes(b"PK\x03\x04 not really a zip archive")
    elif kind == "truncated":
        complete = backup(tmp_path).read_bytes()
        archive.write_bytes(complete[: len(complete) - 30])
    elif kind == "directory":
        archive.mkdir()
    else:
        os.mkfifo(archive, 0o600)  # opening it for reading would block forever
    before = sorted(p.name for p in tmp_path.iterdir())
    code, _, summary = refused(tmp_path, archive)
    expected = (
        "bad_zip_archive" if kind in {"not_a_zip", "truncated"} else "archive_not_regular_file"
    )
    assert code == expected and not summary["destination_created"]
    assert_nothing_created(tmp_path, before)


def test_content_is_never_executed_or_interpreted(tmp_path):
    marker = tmp_path / "executed.marker"
    code = f"import pathlib\npathlib.Path({str(marker)!r}).write_text('executed')\n".encode()
    members = {
        **backup_members(),
        "data.db": code,  # not SQLite: extraction never opens it
        "sitecustomize.py": code,
        "usercustomize.py": code,
        "json.py": code,
        "__init__.py": code,
        "storage/collection1/record1/hook.pb.js": b"$app.dao()",
    }
    archive = backup(tmp_path, members)
    destination, manifest = outputs(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.c11_extract",
            "--archive",
            str(archive),
            "--destination",
            str(destination),
            "--manifest-out",
            str(manifest),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout.splitlines()[-1])["ok"] is True
    assert (destination / "json.py").read_bytes() == code
    assert not marker.exists()


def test_cli_prints_aggregates_only_and_uses_distinct_exit_codes(tmp_path, capsys, monkeypatch):
    secret = "storage/collection1/record1/PRIVATE-CUSTOMER-NAME.pdf"
    archive = backup(tmp_path, {**backup_members(), secret: payload(5000, 5)})
    arguments = ["--archive", str(archive), "--manifest-out"]
    assert main([*arguments, str(tmp_path / "m1.json"), "--destination", str(tmp_path / "d1")]) == 0
    success = capsys.readouterr()
    assert json.loads(success.out)["ok"] is True
    assert main([*arguments, str(tmp_path / "m2.json"), "--destination", str(tmp_path / "d1")]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "destination_exists"
    flip_member_byte(archive, secret)
    assert main([*arguments, str(tmp_path / "m3.json"), "--destination", str(tmp_path / "d3")]) == 1
    crc = capsys.readouterr()
    assert json.loads(crc.out)["error"] in {"member_crc_mismatch", "corrupt_compressed_member"}
    assert not (tmp_path / "d3").exists()
    monkeypatch.setattr(c11_extract, "_remove_created", lambda *_: False)
    assert main([*arguments, str(tmp_path / "m4.json"), "--destination", str(tmp_path / "d4")]) == 3
    left = capsys.readouterr()
    assert json.loads(left.out)["cleanup_error"] == "destination_replaced_or_not_removed"
    for output in (success, crc, left):
        assert "PRIVATE-CUSTOMER-NAME" not in output.out + output.err


def test_sigterm_during_extraction_cleans_up_and_exits_143(tmp_path, monkeypatch, capsys):
    archive = backup(tmp_path)
    original, calls = c11_extract._extract_member, []

    def terminated(*args):
        calls.append(1)
        if len(calls) == 2:
            os.kill(os.getpid(), signal.SIGTERM)
        return original(*args)

    monkeypatch.setattr(c11_extract, "_extract_member", terminated)
    destination, manifest = outputs(tmp_path)
    previous = signal.getsignal(signal.SIGTERM)
    code = main(
        [
            "--archive",
            str(archive),
            "--destination",
            str(destination),
            "--manifest-out",
            str(manifest),
        ]
    )
    assert code == 128 + signal.SIGTERM and signal.getsignal(signal.SIGTERM) is previous
    summary = json.loads(capsys.readouterr().out)
    assert summary["error"] == "interrupted" and summary["destination_removed"]
    assert not destination.exists() and not manifest.exists()
