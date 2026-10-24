"""Offline C11 diagnostics and import primitives. No production apply command.

Only a closed, standalone PB snapshot is accepted. The CLI is read-only apart
from its explicit new report files and never receives a PostgreSQL URL. The
rehearsal and the volume benchmark own their own disposable database. The one
import entry point, ``import_snapshot``, loads records, accounts and settings
(``scripts.c11_auth_import.import_auth``) in a single transaction.

Storage modes: ``directory`` reads and hashes a real ``storage/`` tree;
``manifest`` checks references and declared sizes against the private
extraction manifest ``{archive_sha256, files: [{path, bytes, sha256, crc32}]}``
and never copies or claims file bytes. The verified extraction of a real ZIP
(full mode) is ``scripts/c11_extract.py``; its manifest is the format read
here. This module only inventories an archive (``--archive``).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import resource
import shutil
import signal
import sqlite3
import stat
import time
import zipfile
import zlib
from collections import Counter
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path, PurePosixPath

PROJECT = Path(__file__).resolve().parents[1]
SCHEMA_SHA256 = "9d99e7b54118c41e0a303dd4805b900c7ee7b526b0cc63c83d044b7305b2313d"
ROWID_TABLES = {"users", "buma_article"}
AUTH_MAP = {
    "passwordHash": "password_hash",
    "tokenKey": "token_key",
    "emailVisibility": "email_visibility",
}
AUTH_ARCHIVE = {"lastResetSentAt", "lastVerificationSentAt", "lastLoginAlertSentAt"}
AUTH_ARCHIVE_ORDER = ("lastResetSentAt", "lastVerificationSentAt", "lastLoginAlertSentAt")
SYSTEM_FIELDS = {"id", "created", "updated"}
AUTH_FIELDS = set(AUTH_MAP) | AUTH_ARCHIVE | {"username", "email", "verified"}
ARCHIVE_TABLE = "_buma_c11_rowids"
ARCHIVE_COLUMNS = ("collection_id", "record_id", "source_rowid", "auth_legacy")
DEFAULT_BATCH_SIZE = 5000

# Source versions whose SQLite storage was proven by passing evidence built by
# that exact PocketBase binary. Each entry names its public evidence, the JSON
# path holding the version it proves and, optionally, paths that must hold
# true; all are read again at every check. Never add a version without such a
# report. 0.22.34 (D-0024): the official 0.22.27 and 0.22.34 binaries store
# the same SQLite and storage/ (scripts/c11_version_reference.py); the version
# path is the version that comparison handed to this importer's diagnosis of
# the real_shape fixture built by the 0.22.34 binary.
VALIDATED_SOURCE_VERSIONS = {
    "0.22.27": {
        "evidence": "docs/c11-rehearsal-report.json",
        "version_path": ["diagnostic", "source_version"],
    },
    "0.22.34": {
        "evidence": "docs/c11-pb-0.22.34-reference-report.json",
        "version_path": [
            "binaries",
            "profiles",
            "real_shape",
            "importer_diagnostic",
            "0.22.34",
            "with_source_version",
            "argument",
        ],
        "true_paths": [["conclusion", "importer_can_accept_0_22_34"]],
    },
}

# D-0019: PocketBase <=0.22 stores maxSelect null relations as JSON arrays
# (v0.22.34 models/schema/schema_field.go:715-716 IsMultiple, :159-160
# JSON DEFAULT '[]'). The corrected PPBase stores them as TEXT[]
# (ppbase.models.field_multiplicity; BUMA migration 1789236850 converts a
# database migrated before the correction): each stored list is copied element
# for element (order, repeated elements, any length) into that array column,
# through the path already used by multi-file fields; never flattened. A
# scalar target column stays the explicit blocker unbounded_relation_target_scalar.
UNBOUNDED_RELATION_ARRAYS = True

STRING_KINDS = {"text", "editor", "url", "email"}
LIST_KINDS = {"file", "select", "relation"}
STRING_TYPES = frozenset({"text", "character varying"})
KNOWN_TARGET_TYPES = STRING_TYPES | {
    "integer",
    "bigint",
    "double precision",
    "boolean",
    "timestamp with time zone",
    "jsonb",
}
ELEMENT_TYPES = {
    "varchar": "character varying",
    "text": "text",
    "int4": "integer",
    "int8": "bigint",
    "float8": "double precision",
    "bool": "boolean",
    "timestamptz": "timestamp with time zone",
    "jsonb": "jsonb",
}
INTEGER_RANGES = {"integer": (-(2**31), 2**31), "bigint": (-(2**63), 2**63)}
# PostgreSQL numeric (the jsonb number type): digits before/after the point.
NUMERIC_MAX_INTEGER_DIGITS = 131072
NUMERIC_MAX_SCALE = 16383
JSON_MAX_DEPTH = 256
HEX64 = re.compile(r"[0-9a-f]{64}")
HEX8 = re.compile(r"[0-9a-f]{8}")
# Digests of a 0-byte object, the only ones a 0-byte manifest entry may carry.
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
EMPTY_CRC32 = "00000000"
# The only date text PocketBase 0.22 writes: DateTime.Value/String format UTC
# with DefaultDateLayout (v0.22.34 tools/types/datetime.go:12, 47-53, 69-72).
# ASCII digits only: \d would also accept other Unicode digits.
PB_DATE_LAYOUT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")
ANOMALY_POLICY = "imported_as_is_no_target_or_file_invented_no_reference_removed_or_redirected"
# jsonb representation changes visible to clients. PocketBase 0.22.34 serves
# the stored JSON bytes (tools/types/json_raw.go:26-32); jsonb keeps the value
# but not its text. Duplicate keys would lose the earlier values: blocked.
JSONB_KEY_ORDER = "jsonb_key_order_normalized"
# jsonb rewrites a number written with an exponent (1e2 -> 100, 1.5E-1 ->
# 0.15): same value, but a typed decoder (Dart jsonDecode) reads 1e2 as a
# double and 100 as an int. The only number text jsonb changes; -0 is refused.
JSONB_NUMBER_NOTATION = "jsonb_number_notation_normalized"
JSONB_DUPLICATE_KEYS = "jsonb_duplicate_keys_collapsed"
SQL_NULL_JSON = "sql_null_json_stored_as_jsonb_null"
JSON_WARNING_CODES = (JSONB_KEY_ORDER, JSONB_NUMBER_NOTATION, SQL_NULL_JSON)
# PocketBase 0.22.34 serves a stored json text that PrepareValue would quote
# as a JSON string (models/schema/schema_field.go:312-343): blocked, since the
# jsonb target would hold another value and type.
JSON_SERVED_AS_STRING = "json_text_served_as_string_by_pocketbase"
# First bytes PrepareValue keeps raw when the text is valid JSON (:328-336).
PB_JSON_RAW_FIRST = frozenset('0123456789-"[{')
# Stored list forms that PocketBase 0.22.34 normalizes at every read
# (PrepareValue :351-386 through list.ToUniqueStringSlice, tools/list/
# list.go:89-140) while PPBase serves the stored value. Warnings only: the
# stored value is imported as is.
LIST_DEDUPLICATED = "list_served_deduplicated_by_pocketbase"
SINGLE_LAST_ELEMENT = "single_value_served_as_last_element"
READ_NORMALIZATION_CODES = (LIST_DEDUPLICATED, SINGLE_LAST_ELEMENT)
UNBOUNDED_SCALAR = "unbounded_relation_target_scalar"
# Array target while UNBOUNDED_RELATION_ARRAYS is off (kept for a deliberate
# rollback of the flag; never raised while it is on).
UNBOUNDED_NOT_ENABLED = "unbounded_relation_arrays_not_enabled"
# Relation definitions whose maxSelect has no agreed meaning, refused whatever
# the target (``relation_definition_problem``); none in the export.
RELATION_MAX_SELECT_NOT_POSITIVE = "relation_max_select_not_positive_requires_decision"
RELATION_MAX_SELECT_ABSENT = "relation_max_select_absent_requires_decision"
RELATION_DEFINITION_CODES = (RELATION_MAX_SELECT_NOT_POSITIVE, RELATION_MAX_SELECT_ABSENT)
# Independent recount of array relations after the load (``recount_relation_arrays``).
RELATION_ARRAY_RECOUNT = "relation_array_recount_mismatch"
MULTISET_MODULUS = 2**256
# Rows per round trip of the end-of-load re-read (server-side portal).
FINAL_PREFETCH = 10000
_FAILED = object()
# The only non-internal triggers of the migrated target, and their exact
# definition (pb_migrations/1789236848_c10_sqlite_rowid.py): BEFORE INSERT FOR
# EACH ROW (tgtype ROW|BEFORE|INSERT = 7), enabled ('O'), calling the
# argument-less public.buma_allocate_sqlite_rowid(), not a constraint trigger,
# not deferrable, no WHEN clause, no column list, no trigger argument. Any
# other trigger, and any enabled event trigger, is refused before the first
# write. The function body is not compared: whatever it changes is caught by
# the readback layers (proved at runtime by the volume benchmark).
ROWID_TRIGGER = "buma_sqlite_rowid"
ROWID_TRIGGER_DEFINITION = {
    "type": 7,
    "enabled": "O",
    "function": "public.buma_allocate_sqlite_rowid",
    "function_arguments": 0,
    "is_constraint": False,
    "deferrable": False,
    "initially_deferred": False,
    "arguments": 0,
    "has_when": False,
    "has_columns": False,
}


class ImportBlocked(ValueError):
    """An unresolved condition, never a request to repair source records.

    ``str(exc)`` is the stable blocking code; ``details`` only carries schema
    names, SQLSTATE codes and counts, never record values. It is never chained
    to the error that revealed it: a driver or decoder error can carry record
    values (PostgreSQL DETAIL "Failing row contains (...)" or "Key (...)=(...)",
    JSONDecodeError.doc, UnicodeEncodeError.object). Every ImportBlocked is
    therefore raised after the ``except`` block that detected the condition, so
    that ``__cause__`` and ``__context__`` stay None and a rendered traceback
    shows no source value.
    """

    def __init__(self, code: str, **details):
        super().__init__(code)
        self.code = code
        self.details = details


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def file_checksums(path: Path) -> tuple[str, str, int]:
    """SHA-256, CRC-32 (8 hex, as in ZIP members) and size in one stream."""
    sha, crc, size = hashlib.sha256(), 0, 0
    with path.open("rb") as stream:
        while chunk := stream.read(1 << 20):
            sha.update(chunk)
            crc = zlib.crc32(chunk, crc)
            size += len(chunk)
    return sha.hexdigest(), f"{crc:08x}", size


def json_bytes(value, *, ascii: bool = False) -> bytes:
    """Complete UTF-8 JSON document, serialized before any file is created.

    ``ascii`` escapes every non-ASCII character (``\\uXXXX``): exact, and the
    only form that can carry a lone surrogate (an undecodable file name, a
    JSON ``\\ud800`` escape) in a valid UTF-8 file. Used for private details.
    """
    return (json.dumps(value, ensure_ascii=ascii, indent=2, allow_nan=False) + "\n").encode()


def write_bytes_new(path: Path, data: bytes) -> None:
    """Create a new 0600 file; on any write failure remove the partial file."""
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
    except BaseException:
        path.unlink(missing_ok=True)  # only the file this call just created
        raise


def write_json_new(path: Path, value: dict, *, ascii: bool = False) -> None:
    write_bytes_new(path, json_bytes(value, ascii=ascii))


def inventory_fingerprint(files: dict) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


def peak_rss_kib() -> int:
    """Peak resident set size of this process image, in KiB.

    Linux VmHWM belongs to the current address space; ru_maxrss can carry the
    high-water mark of the parent that forked this process before exec.
    """
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def archive_blockers(archive: zipfile.ZipFile) -> tuple[list[str], list[str], int]:
    """Member-level refusals of the archive inventory.

    ``scripts.c11_extract`` runs ``inspect_archive`` (hence these checks) before
    its own stricter ones. Returns (sorted blocking codes, data.db member
    names, declared bytes).
    """
    blockers, names, databases = [], set(), []
    total_bytes = 0
    for member in archive.infolist():
        name = member.filename
        parts = PurePosixPath(name).parts
        if name.startswith("/") or ".." in parts or "\\" in name or ":" in name:
            blockers.append("unsafe_archive_member")
        if name in names:
            blockers.append("duplicate_archive_member")
        names.add(name)
        if stat.S_ISLNK(member.external_attr >> 16):
            blockers.append("symlink_in_archive")
        if member.flag_bits & 1:
            blockers.append("encrypted_archive_member")
        if name.endswith(("data.db-wal", "data.db-journal")) and member.file_size:
            blockers.append("live_or_incomplete_sqlite_snapshot")
        if not member.is_dir():
            total_bytes += member.file_size
            if parts and parts[-1] == "data.db":
                databases.append(name)
    if len(databases) != 1:
        blockers.append("exactly_one_data_db_required")
    return sorted(set(blockers)), databases, total_bytes


def inspect_archive(path: Path) -> dict:
    """Inspect a large ZIP without extracting data or reading database records.

    Central-directory sizes are declared values, not CRC/content verification.
    The archive checksum streams the compressed file with bounded memory.
    """
    initial = path.stat()
    with zipfile.ZipFile(path) as archive:
        blockers, databases, total_bytes = archive_blockers(archive)
        members = len({member.filename for member in archive.infolist()})
    checksum = digest(path)
    final = path.stat()
    if (initial.st_size, initial.st_mtime_ns) != (final.st_size, final.st_mtime_ns):
        blockers = sorted({*blockers, "archive_changed_during_inventory"})
    return {
        "mode": "archive_read_only_inventory",
        "production_authorized": False,
        "archive_sha256": checksum,
        "archive_bytes": path.stat().st_size,
        "members": members,
        "declared_uncompressed_bytes": total_bytes,
        "data_db_members": databases,
        "blockers": blockers,
        "ready_for_offline_extraction_review": not blockers,
        "ready_for_isolated_rehearsal": False,
        "content_crc_verified": False,
        "database_schema_and_records_checked": False,
    }


def load_schema(path: Path) -> list[dict]:
    if digest(path) != SCHEMA_SHA256:
        raise ImportBlocked("schema_export_checksum_mismatch")
    return json.loads(path.read_text())


def definitions_fingerprint(definitions: list[dict]) -> str:
    """SHA-256 of the parsed definitions (canonical JSON), whatever their file.

    Recorded by the diagnosis and required again by the load: the
    definitions handed to ``load_isolated`` must be exactly the diagnosed ones.
    """
    canonical = json.dumps(definitions, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def source_version_status(version: str | None) -> tuple[str | None, str | None]:
    """Return (blocking code or None, public evidence path) for a source version.

    The registry entry is accepted only when its evidence report exists, passed
    and states that exact version; nothing is inferred from file names.
    """
    if not version:
        return "source_pocketbase_version_unknown", None
    entry = VALIDATED_SOURCE_VERSIONS.get(version)
    if entry is None:
        return "source_pocketbase_version_unvalidated", None

    def at(document, path):
        for key in path:
            document = document[key]
        return document

    try:
        evidence = json.loads((PROJECT / entry["evidence"]).read_text())
        stated = at(evidence, entry["version_path"])
        proofs = [at(evidence, path) for path in entry.get("true_paths", ())]
        status = evidence.get("status")
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
        return "source_version_evidence_invalid", entry["evidence"]
    if status != "passed" or stated != version or any(proof is not True for proof in proofs):
        return "source_version_evidence_invalid", entry["evidence"]
    return None, entry["evidence"]


@contextmanager
def snapshot(data: Path, *, worker_threads: bool = False):
    """Read-only, immutable SQLite connection to a closed snapshot.

    ``worker_threads`` lets the loader read in one worker thread at a time
    while the event loop waits on PostgreSQL; the connection is never shared
    concurrently.
    """
    path = data / "data.db"
    if not path.is_file() or path.is_symlink() or data.is_symlink():
        raise ImportBlocked("standalone_data_db_required")
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ImportBlocked("live_or_incomplete_sqlite_snapshot")
    # immutable avoids creating SHM/WAL and does not acquire a write lock.
    db = sqlite3.connect(
        path.resolve().as_uri() + "?mode=ro&immutable=1",
        uri=True,
        check_same_thread=not worker_threads,
    )
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Value conversion: exact source value -> exact PostgreSQL value, or a code.
# ---------------------------------------------------------------------------


def _reject_constant(_):
    raise ImportBlocked("non_finite_json")


def strict_json(value):
    return json.loads(value, parse_constant=_reject_constant)


def _check_pg_string(value: str, nul_code: str) -> None:
    """PostgreSQL text and jsonb refuse U+0000 and need valid UTF-8."""
    if "\x00" in value:
        raise ImportBlocked(nul_code)
    if not value.isascii():  # O(1) in CPython: most values skip the encoding
        encodable = True
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            # Lone surrogates (e.g. a JSON \ud800 escape) have no UTF-8 encoding.
            # The error holds the whole value (.object): never chained.
            encodable = False
        if not encodable:
            raise ImportBlocked("invalid_unicode_for_postgresql")


def _check_json_number(number: Decimal) -> None:
    if not number.is_finite():
        raise ImportBlocked("non_finite_json")
    if number.is_zero() and number.is_signed():
        # PostgreSQL numeric/jsonb has no negative zero ('-0'::jsonb is 0).
        raise ImportBlocked("json_number_not_representable")
    _, digits, exponent = number.as_tuple()
    before = max(len(digits) + exponent, 0)
    after = max(-exponent, 0)
    if before > NUMERIC_MAX_INTEGER_DIGITS or after > NUMERIC_MAX_SCALE:
        raise ImportBlocked("json_number_outside_target_range")


class _InvalidJsonConstant(ValueError):
    """NaN or Infinity: not JSON for Go's decoder, hence for PocketBase."""


def _invalid_constant(_):
    raise _InvalidJsonConstant


def _parse_exact_json(text: str, invalid_code: str = "invalid_json", **hooks):
    """Parse JSON with exact Decimal numbers; never chained to the decoder error.

    Any text that is not JSON (NaN and Infinity included) raises
    ``invalid_code``; nesting beyond the parser recursion limit raises
    ``json_nesting_too_deep`` (refused anyway beyond JSON_MAX_DEPTH). A hook
    may raise its own ImportBlocked. The decoder error (whose ``doc`` is the
    whole text) is never attached to the raised code.
    """
    options = {
        "parse_float": Decimal,
        "parse_int": Decimal,
        "parse_constant": _invalid_constant,
        **hooks,
    }
    failure = None
    try:
        parsed = json.loads(text, **options)
    except ImportBlocked as exc:
        failure = exc.code
    except RecursionError:
        failure = "json_nesting_too_deep"
    except ValueError:
        failure = invalid_code
    if failure is not None:
        raise ImportBlocked(failure)
    return parsed


def check_json_text(text: str) -> None:
    """Refuse a stored json text unless PocketBase serves it raw and jsonb can
    store exactly its value; never repair it.

    PocketBase 0.22.34 PrepareValue (models/schema/schema_field.go:312-343)
    keeps a text raw only when it is exactly null, true or false, or starts
    with 0-9, '-', '"', '[' or '{' and is valid JSON (is.JSON, Go's decoder).
    Any other text (empty, leading whitespace, 'true ', invalid JSON, NaN) is
    served as a JSON string: ``json_text_served_as_string_by_pocketbase``. A
    duplicate object key is refused (``jsonb_duplicate_keys_collapsed``):
    jsonb would keep only its last value, while PocketBase serves every
    occurrence. Nesting deeper than JSON_MAX_DEPTH is refused explicitly:
    PostgreSQL's parser depth depends on its stack setting and the exact
    comparison of the readback is recursive. Real BUMA json values are shallow
    configurations.
    """
    if text not in ("null", "true", "false") and (not text or text[0] not in PB_JSON_RAW_FIRST):
        raise ImportBlocked(JSON_SERVED_AS_STRING)
    duplicate = False

    def pairs_hook(pairs):
        nonlocal duplicate
        result = dict(pairs)
        if len(result) != len(pairs):
            duplicate = True  # judged once the whole text is known to be JSON
        return result

    parsed = _parse_exact_json(text, JSON_SERVED_AS_STRING, object_pairs_hook=pairs_hook)
    if duplicate:
        raise ImportBlocked(JSONB_DUPLICATE_KEYS)
    stack = [(parsed, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, str):
            _check_pg_string(item, "nul_in_postgresql_jsonb")
        elif isinstance(item, Decimal):
            _check_json_number(item)
        elif isinstance(item, (list, dict)):
            if depth > JSON_MAX_DEPTH:
                raise ImportBlocked("json_nesting_too_deep")
            if isinstance(item, list):
                stack.extend((value, depth + 1) for value in item)
            else:
                for key, value in item.items():
                    _check_pg_string(key, "nul_in_postgresql_jsonb")
                    stack.append((value, depth + 1))


def go_float_text(value: float) -> str:
    """strconv.FormatFloat(value, 'g', -1, 64) of a finite float.

    The text Go's database/sql gives PocketBase for a SQLite REAL read into a
    string (convertAssign/asString, through dbx.NullStringMap). Shortest
    round-trip digits (those of repr()); exponent form when the decimal
    exponent is below -4 or at least 6 (Go uses precision 6 for that choice in
    the shortest mode), with at least two exponent digits.
    """
    sign, digits, exponent = Decimal(repr(value)).as_tuple()
    digits = list(digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    prefix = "-" if sign else ""
    if digits == [0]:
        return prefix + "0"
    text = "".join(map(str, digits))
    point = len(digits) + exponent  # digits before the decimal point
    power = point - 1
    if power < -4 or power >= 6:
        mantissa = text[0] + ("." + text[1:] if len(text) > 1 else "")
        return f"{prefix}{mantissa}e{'-' if power < 0 else '+'}{abs(power):02d}"
    if point > 0:
        whole, fraction = text[:point] + "0" * max(point - len(text), 0), text[point:]
    else:
        whole, fraction = "0", "0" * -point + text
    return prefix + whole + ("." + fraction if fraction else "")


def json_text(value) -> str:
    """Exact JSON text PocketBase 0.22.34 reads and serves for a stored json
    value, checked for the jsonb target (``check_json_text``).

    PB reads every column as a string through dbx.NullStringMap (daos/
    record.go:75-80; models/record.go:48-58, 65-91) and normalizes it with
    PrepareValue (models/record.go:292-306; models/schema/schema_field.go:
    312-343). The column is ``JSON DEFAULT NULL`` (schema_field.go:156-157), of
    NUMERIC affinity in SQLite: a top-level number is stored as INTEGER or
    REAL. database/sql formats an INTEGER with strconv.FormatInt and a REAL
    with strconv.FormatFloat(v, 'g', -1, 64) (``go_float_text``); that text is
    what PB serves (e.g. 1.2345675e+06 for 1234567.5), the stored value is
    kept. A text value is kept verbatim when PB serves it raw (jsonb keeps its
    value, not its text: see ``jsonb_text_changes``). SQL NULL
    (nullStringMapValue gives nil) scans to an empty JsonRaw served as
    ``null`` (tools/types/json_raw.go:26-29, 56-61): the NOT NULL jsonb target
    of PPBase can only store JSON null, which the diagnosis reports as
    ``sql_null_json_stored_as_jsonb_null`` (same value in responses, not the
    same SQL value for filters).
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        raise ImportBlocked("invalid_json")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            # Go would give +Inf/-Inf, texts that PrepareValue serves as strings.
            raise ImportBlocked("non_finite_json")
        if value == 0 and math.copysign(1.0, value) < 0:
            raise ImportBlocked("json_number_not_representable")
        return go_float_text(value)
    if not isinstance(value, str):
        raise ImportBlocked("invalid_json")
    check_json_text(value)
    return value


def _jsonb_key(key: str) -> tuple[int, bytes]:
    # jsonb sorts object keys by UTF-8 length, then bytes (uniqueifyJsonbObject).
    encoded = key.encode("utf-8", "surrogatepass")
    return len(encoded), encoded


def jsonb_text_changes(text: str) -> frozenset:
    """Client-visible rewrites by jsonb of a JSON text PocketBase serves raw.

    ``jsonb_key_order_normalized``: an object would be served with its keys in
    jsonb order (UTF-8 length, then bytes) instead of their stored order;
    ``jsonb_number_notation_normalized``: a number written with an exponent
    (1e2, 1.5E-1), which jsonb renders positionally (100, 0.15); every other
    number text is kept (the scale of 1.50 included). Values are unchanged.
    PocketBase 0.22.34 serves the stored bytes (tools/types/json_raw.go:
    26-32). Duplicate keys are refused by ``check_json_text`` beforehand.
    """
    changes = set()

    def pairs_hook(pairs):
        if JSONB_KEY_ORDER not in changes:
            keys = list(dict.fromkeys(key for key, _ in pairs))
            if keys != sorted(keys, key=_jsonb_key):
                changes.add(JSONB_KEY_ORDER)
        return dict(pairs)

    def number_hook(token):
        if "e" in token or "E" in token:
            changes.add(JSONB_NUMBER_NOTATION)
        return Decimal(token)

    _parse_exact_json(text, object_pairs_hook=pairs_hook, parse_float=number_hook)
    return frozenset(changes)


def array_value(value) -> list[str]:
    failure = None
    try:
        parsed = strict_json(value) if isinstance(value, str) else value
    except ImportBlocked as exc:
        failure = exc.code
    except (ValueError, RecursionError):  # the decoder error holds the value
        failure = "invalid_array"
    if failure is not None:
        raise ImportBlocked(failure)
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        raise ImportBlocked("invalid_array")
    return parsed


def date_value(value, *, required=False):
    """Exact instant of a stored PB 0.22 date, or None for "" when optional.

    PocketBase writes every date as ``YYYY-MM-DD HH:MM:SS.mmmZ`` in UTC
    (DateTime.Value/String, v0.22.34 tools/types/datetime.go:12, 47-53,
    69-72). Its reader also tolerates other layouts through cast.ToTime and
    yields the zero time when nothing matches (:82-91). Here any other text
    is ``date_not_pocketbase_layout``: no offset conversion, no alternative
    notation and no digit below the millisecond is ever silently accepted or
    truncated. Real backup: every non-empty date has this layout.
    """
    if value == "" and not required:
        return None
    if not isinstance(value, str) or not value:
        raise ImportBlocked("invalid_date")
    if not PB_DATE_LAYOUT.fullmatch(value):
        raise ImportBlocked("date_not_pocketbase_layout")
    instant = None
    try:
        # The layout is fixed: exactly three fraction digits, UTC.
        instant = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        pass  # e.g. 2024-02-30 or 24:00; the message may quote the value
    if instant is None:
        raise ImportBlocked("invalid_date")
    return instant


def required_date_value(value):
    return date_value(value, required=True)


def text_value(value) -> str:
    if not isinstance(value, str):
        raise ImportBlocked("invalid_text")
    _check_pg_string(value, "nul_in_postgresql_text")
    return value


def record_id_value(value) -> str:
    if not isinstance(value, str) or not value:
        raise ImportBlocked("invalid_record_id")
    return text_value(value)


def auth_text_value(value) -> str:
    if not isinstance(value, str):
        raise ImportBlocked("invalid_auth_field")
    return text_value(value)


def text_array_value(value) -> list[str]:
    """JSON list of strings, element order and duplicates kept as stored.

    Each element is checked like a text value: a JSON ``\\u0000`` or a lone
    surrogate escape (``\\ud800``) cannot be stored in PostgreSQL text[].
    """
    items = array_value(value)
    for item in items:
        _check_pg_string(item, "nul_in_postgresql_text")
    return items


def unbounded_relation_value(value) -> list[str]:
    if not UNBOUNDED_RELATION_ARRAYS:
        # Never flatten, pick one ID or copy the JSON text into a scalar.
        raise ImportBlocked(UNBOUNDED_SCALAR)
    return text_array_value(value)


def bool_value(value) -> bool:
    if value not in (0, 1):
        raise ImportBlocked("invalid_boolean")
    return bool(value)


def double_value(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ImportBlocked("invalid_number")
    if not math.isfinite(value):
        raise ImportBlocked("invalid_number")
    if isinstance(value, int) and int(float(value)) != value:
        raise ImportBlocked("inexact_target_double")
    return float(value)


def integer_value(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ImportBlocked("invalid_number")
    if not math.isfinite(value):
        raise ImportBlocked("invalid_number")
    if value != int(value) or not -(2**31) <= value < 2**31:
        raise ImportBlocked("integer_outside_target_range")
    return int(value)


def rowid_value(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ImportBlocked("invalid_source_rowid")
    return value


def unsupported_value(_):
    raise ImportBlocked("unsupported_field_type")


def _multiplicity():
    """The PPBase rule (D-0019), ``ppbase.models.field_multiplicity``.

    Imported on first use: the CLI and the staging cleanup do not need it, and
    a PPBase without the rule fails here, loudly, instead of reading a
    maxSelect null relation as single.
    """
    from ppbase.models import field_multiplicity

    return field_multiplicity


def relation_definition_problem(field: dict) -> str | None:
    """Refusal of a relation whose maxSelect has no agreed meaning, else None.

    Options as the PPBase rule reads them (``field_options``).
    ``relation_max_select_not_positive_requires_decision`` (0 or less):
    PocketBase 0.22.34 refuses to save it (models/schema/schema_field.go:709,
    NilOrNotEmpty and Min(minSelect >= 0)), stores it single (:715-716, :163)
    and then refuses any non-empty value (forms/validators/record_data.go:
    376-377); its 0.23 upgrade makes it unlimited (v0.23.0
    migrations/1717233556_v0.23_migrate.go:844-848) and PPBase single with a
    limit of 1 (``relation_max_select``): three meanings, none chosen here.
    ``relation_max_select_absent_requires_decision``: without the key,
    PocketBase 0.22.34 decodes a nil pointer (multiple, :693 and :716), PPBase
    single (documented difference of its rule). PocketBase 0.22 always writes
    the key (no omitempty, :693); every relation of the export has it, none
    is 0 or less (188 are 1, 3 are null).
    """
    if field["type"] != "relation":
        return None
    options = _multiplicity().field_options(field)
    if "maxSelect" not in options:
        return RELATION_MAX_SELECT_ABSENT
    maximum = options["maxSelect"]
    if isinstance(maximum, (int, float)) and not isinstance(maximum, bool) and maximum <= 0:
        return RELATION_MAX_SELECT_NOT_POSITIVE
    return None


def relation_unbounded(field: dict) -> bool:
    """Unlimited multiple relation: maxSelect null (D-0019).

    The PPBase rule: ``relation_max_select`` is None, i.e. PocketBase 0.22.34
    RelationOptions.IsMultiple with a nil MaxSelect (models/schema/
    schema_field.go:693, 715-716), stored as a JSON list. maxSelect 0 or less
    and an absent key are not unbounded here: ``relation_definition_problem``.
    """
    if field["type"] != "relation" or relation_definition_problem(field):
        return False
    rule = _multiplicity()
    return rule.relation_max_select(rule.field_options(field)) is None


def source_multiple(field: dict) -> bool:
    """Whether PB 0.22 stores this field as a JSON list: the PPBase rule.

    ``field_is_multiple`` of ``ppbase.models.field_multiplicity`` (D-0019):
    select and file maxSelect > 1, relation maxSelect null or > 1, which is
    PocketBase 0.22.34 IsMultiple (models/schema/schema_field.go:626-627,
    667-668, 715-716) for every definition PocketBase 0.22 writes, maxSelect 0
    or less included (single for both, and refused for import). The one form
    where they part, a relation without the maxSelect key, is read as 0.22.34
    stored it (a list) and refused for import (``relation_definition_problem``).
    """
    if relation_definition_problem(field) == RELATION_MAX_SELECT_ABSENT:
        return True
    return _multiplicity().field_is_multiple(field)


@dataclass(frozen=True)
class Step:
    source: str
    column: str
    convert: object
    types: frozenset
    array: bool = False
    unbounded: bool = False
    refusal: str | None = None


def refused(code: str):
    """Converter refusing every value of a field with ``code``."""

    def refuse(_):
        raise ImportBlocked(code)

    return refuse


def field_step(field: dict) -> Step:
    kind, options = field["type"], field.get("options") or {}
    name = field.get("name", "")
    if kind == "json":
        return Step(name, name, json_text, frozenset({"jsonb"}))
    if kind == "date":
        return Step(name, name, date_value, frozenset({"timestamp with time zone"}))
    if kind == "bool":
        return Step(name, name, bool_value, frozenset({"boolean"}))
    if kind == "number":
        if options.get("onlyInt"):
            return Step(name, name, integer_value, frozenset({"integer"}))
        return Step(name, name, double_value, frozenset({"double precision"}))
    problem = relation_definition_problem(field)
    if problem is not None:
        array = source_multiple(field)
        return Step(name, name, refused(problem), STRING_TYPES, array, refusal=problem)
    if relation_unbounded(field):
        return Step(name, name, unbounded_relation_value, STRING_TYPES, True, True)
    if kind in LIST_KINDS and source_multiple(field):
        return Step(name, name, text_array_value, STRING_TYPES, True)
    if kind in STRING_KINDS | LIST_KINDS:
        return Step(name, name, text_value, STRING_TYPES)
    return Step(name, name, unsupported_value, frozenset())


AUTH_STEPS = (
    Step("username", "username", auth_text_value, STRING_TYPES),
    Step("email", "email", auth_text_value, STRING_TYPES),
    Step("emailVisibility", "email_visibility", bool_value, frozenset({"boolean"})),
    Step("verified", "verified", bool_value, frozenset({"boolean"})),
    Step("tokenKey", "token_key", auth_text_value, STRING_TYPES),
    Step("passwordHash", "password_hash", auth_text_value, STRING_TYPES),
)


class RowPlan:
    """Fixed column order and converters for one base/auth collection."""

    def __init__(self, collection: dict):
        timestamp = frozenset({"timestamp with time zone"})
        steps = [
            Step("id", "id", record_id_value, STRING_TYPES),
            Step("created", "created", required_date_value, timestamp),
            Step("updated", "updated", required_date_value, timestamp),
        ]
        self.known = {f["name"] for f in collection["schema"]} | SYSTEM_FIELDS | {"__c11_rowid"}
        self.auth = collection["type"] == "auth"
        if self.auth:
            self.known |= AUTH_FIELDS
            steps += AUTH_STEPS
        steps += [field_step(field) for field in collection["schema"]]
        if collection["name"] in ROWID_TABLES:
            steps.append(Step("__c11_rowid", "rowid", rowid_value, frozenset({"bigint"})))
        self.steps = tuple(steps)
        self.columns = tuple(step.column for step in steps)

    def convert(self, row: dict, problems: list | None = None):
        """Return (values in column order, auth archive) or raise the first code.

        With ``problems``, each failure is recorded as (column, code) and its
        value is a sentinel, so every other column is still checked.
        """
        if row.keys() - self.known:
            if problems is None:
                raise ImportBlocked("unmapped_source_columns")
            problems.append((None, "unmapped_source_columns"))
        values = []
        for step in self.steps:
            value = row[step.source]  # a missing source column is a KeyError
            code = None
            try:
                converted = step.convert(value)
            except ImportBlocked as exc:
                code = exc.code
            except (ValueError, TypeError, OverflowError, RecursionError):
                code = "unmappable_value"  # the error may quote the value: not chained
            if code is None:
                values.append(converted)
                continue
            if problems is None:
                raise ImportBlocked(code)
            problems.append((step.column, code))
            values.append(_FAILED)
        archive = {key: row[key] for key in AUTH_ARCHIVE_ORDER if key in row}
        return values, archive


def auth_legacy_text(archive: dict) -> str:
    """jsonb text of the archived PB 0.22 auth dates, raw source values.

    Checked identically by the diagnosis and before each COPY: a value that
    jsonb cannot hold exactly (U+0000, a non-finite number) blocks the field
    ``auth_legacy`` instead of failing in PostgreSQL.
    """
    if not archive:
        return "{}"
    text = None
    try:
        text = json.dumps(archive, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        pass  # the message may quote the value: never chained
    if text is None:
        raise ImportBlocked("invalid_auth_field")
    check_json_text(text)
    return text


def field_value(field, value):
    """Python value of one field (JSON parsed); raises a code, never repairs."""
    if field["type"] == "json":
        return strict_json(json_text(value))
    return field_step(field).convert(value)


def mapped_row(collection: dict, row: dict) -> tuple[dict, dict]:
    """Python values of one source row (JSON parsed); raises the first code."""
    plan = RowPlan(collection)
    values, archive = plan.convert(row)
    result = dict(zip(plan.columns, values, strict=True))
    for field in collection["schema"]:
        if field["type"] == "json":
            result[field["name"]] = strict_json(result[field["name"]])
    return result, archive


def stored_references(value, multiple: bool) -> list[str]:
    """References exactly as PB 0.22 stored them; never split, trim or repair."""
    if multiple:
        try:
            return array_value(value)
        except ImportBlocked:
            return []  # the conversion of this value already reports the code
    return [value] if isinstance(value, str) and value else []


def nonzero_uniques(items) -> list[str]:
    """list.NonzeroUniques (tools/list/list.go:89-108): no "", first occurrences."""
    return [item for item in dict.fromkeys(items) if item != ""]


def pocketbase_list_changed(items: list[str]) -> bool:
    """PocketBase 0.22.34 serves a stored list without "" and duplicates."""
    return "" in items or len(set(items)) != len(items)


def pocketbase_single_value(value):
    """Value PocketBase 0.22.34 serves for a single select, file or relation.

    PrepareValue (models/schema/schema_field.go:351-386) serves the last
    element of list.ToUniqueStringSlice (tools/list/list.go:110-140): a stored
    text containing '[' that Go unmarshals as an array of strings (a JSON null
    element being "") is served as its last non-empty unique element, or "";
    any other text as stored (after a failed unmarshal the text itself is
    appended, hence last). Used to warn only: the stored text is imported.
    """
    if not isinstance(value, str) or "[" not in value:
        return value
    parsed = None
    try:
        parsed = json.loads(value, parse_constant=_invalid_constant)
    except (ValueError, RecursionError):
        pass
    if not isinstance(parsed, list) or any(
        item is not None and not isinstance(item, str) for item in parsed
    ):
        return value
    uniques = nonzero_uniques("" if item is None else item for item in parsed)
    return uniques[-1] if uniques else ""


# ---------------------------------------------------------------------------
# Storage: real directory or private extraction manifest.
# ---------------------------------------------------------------------------


def storage_inventory(root: Path) -> dict[str, dict]:
    if not root.is_dir() or root.is_symlink():
        raise ImportBlocked("local_storage_directory_required")
    found = {}
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in directories + files:
            path = Path(directory) / name
            if path.is_symlink():
                raise ImportBlocked("symlink_in_storage")
            if path.is_dir():
                continue
            if not path.is_file():
                raise ImportBlocked("non_regular_storage_object")
            relative = path.relative_to(root).as_posix()
            found[relative] = {"bytes": path.stat().st_size, "sha256": digest(path)}
    return dict(sorted(found.items()))


def _safe_manifest_path(path) -> tuple[str, ...]:
    if not isinstance(path, str) or not path or path.startswith("/"):
        raise ImportBlocked("manifest_unsafe_entry")
    if "\\" in path or "\x00" in path or ":" in path:
        raise ImportBlocked("manifest_unsafe_entry")
    parts = tuple(path.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise ImportBlocked("manifest_unsafe_entry")
    return parts


def read_manifest(path: Path, data_db: Path) -> dict:
    """Validate the private manifest and the data.db it describes; no file bytes.

    Returns the storage inventory in the ``storage_inventory`` format (keys
    relative to storage/) and a summary without any path or record value. A
    0-byte entry must carry the SHA-256 and CRC-32 of empty content; a root
    SQLite sidecar (-wal, -journal) must be empty.
    """
    if not path.is_file() or path.is_symlink():
        raise ImportBlocked("manifest_file_required")
    raw = path.read_bytes()
    manifest = None
    try:
        manifest = json.loads(raw)
    except ValueError:
        pass  # JSONDecodeError.doc is the private manifest: never chained
    if not isinstance(manifest, dict) or set(manifest) != {"archive_sha256", "files"}:
        raise ImportBlocked("manifest_invalid")
    if not isinstance(manifest["archive_sha256"], str) or not HEX64.fullmatch(
        manifest["archive_sha256"]
    ):
        raise ImportBlocked("manifest_invalid")
    if not isinstance(manifest["files"], list):
        raise ImportBlocked("manifest_invalid")
    seen, root, storage = set(), {}, {}
    for entry in manifest["files"]:
        if not isinstance(entry, dict) or set(entry) != {"path", "bytes", "sha256", "crc32"}:
            raise ImportBlocked("manifest_invalid")
        size, sha, crc = entry["bytes"], entry["sha256"], entry["crc32"]
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ImportBlocked("manifest_invalid")
        if not isinstance(sha, str) or not HEX64.fullmatch(sha):
            raise ImportBlocked("manifest_invalid")
        if not isinstance(crc, str) or not HEX8.fullmatch(crc):
            raise ImportBlocked("manifest_invalid")
        if size == 0 and (sha, crc) != (EMPTY_SHA256, EMPTY_CRC32):
            # An empty object has exactly one SHA-256 and one CRC-32.
            raise ImportBlocked("manifest_invalid")
        parts = _safe_manifest_path(entry["path"])
        if entry["path"] in seen:
            raise ImportBlocked("manifest_duplicate_entry")
        seen.add(entry["path"])
        if len(parts) == 1:
            root[parts[0]] = entry
        elif parts[0] == "storage":
            storage["/".join(parts[1:])] = {"bytes": size, "sha256": sha}
        else:
            raise ImportBlocked("unexpected_manifest_entry")
    database = root.get("data.db")
    if database is None:
        raise ImportBlocked("manifest_data_db_entry_required")
    sidecars = sorted(name for name in root if name.endswith(("-wal", "-journal")))
    if any(root[name]["bytes"] for name in sidecars):
        raise ImportBlocked("live_or_incomplete_sqlite_snapshot")
    if not data_db.is_file() or data_db.is_symlink():
        raise ImportBlocked("standalone_data_db_required")
    sha, crc, size = file_checksums(data_db)
    if (sha, crc, size) != (database["sha256"], database["crc32"], database["bytes"]):
        raise ImportBlocked("data_db_manifest_mismatch")
    inventory = dict(sorted(storage.items()))
    return {
        "storage": inventory,
        "summary": {
            "sha256": hashlib.sha256(raw).hexdigest(),
            "archive_sha256": manifest["archive_sha256"],
            "entries": len(manifest["files"]),
            "root_entries": sorted(root),
            "storage_entries": len(inventory),
            "storage_bytes_declared": sum(item["bytes"] for item in inventory.values()),
            "data_db_matches_manifest": True,
            "empty_sqlite_sidecars": sidecars,
        },
    }


def directory_manifest(root: Path) -> dict:
    """Private-manifest format for a closed local directory (synthetic tests).

    ``archive_sha256`` is the digest of the listing itself: no ZIP exists.
    """
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ImportBlocked("symlink_in_storage")
        if path.is_file():
            sha, crc, size = file_checksums(path)
            files.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "bytes": size,
                    "sha256": sha,
                    "crc32": crc,
                }
            )
    listing = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return {"archive_sha256": listing, "files": files}


def path_present(path: Path) -> bool:
    """Exists, or is a symbolic link (even a dangling one)."""
    return path.exists() or path.is_symlink()


def storage_source(data: Path, manifest: Path | None) -> tuple[dict, dict]:
    """(inventory, report fields) for one diagnosis or staging."""
    if manifest is None:
        files = storage_inventory(data / "storage")
        return files, {
            "storage_mode": "directory",
            "file_bytes_verified": True,
            "file_sizes_source": "storage_directory",
        }
    if path_present(data / "storage"):
        raise ImportBlocked("storage_directory_and_manifest_both_supplied")
    checked = read_manifest(manifest, data / "data.db")
    return checked["storage"], {
        "storage_mode": "manifest",
        "file_bytes_verified": False,
        "file_sizes_source": "manifest_declared",
        "manifest": checked["summary"],
    }


def file_key(collection: str, record: str, filename: str) -> str:
    for part in (collection, record, filename):
        if not part or part in {".", ".."} or any(c in part for c in ("/", "\\", "\x00")):
            raise ImportBlocked("unsafe_storage_reference")
    return f"{collection}/{record}/{filename}"


def source_rows(db, collection):
    return db.execute(
        f"SELECT _rowid_ AS __c11_rowid, * FROM {quote(collection['name'])} ORDER BY _rowid_"
    )


def idempotency_key_conflicts(db) -> int:
    """Groups that the frozen source unique index would reject (D-0022).

    Only idx_transaction_idempotency exists in the source; SQLite evaluates its
    LIKE predicate here with the same ASCII case folding as PocketBase.
    """
    return db.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM buma_transaction WHERE system_id LIKE 'idem:%' "
        "GROUP BY system_id HAVING COUNT(*)>1)"
    ).fetchone()[0]


# ---------------------------------------------------------------------------
# Target columns: introspection, preflight and per-value checks.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetColumn:
    name: str
    type: str
    array: bool
    length: int | None
    not_null: bool
    has_default: bool


INTROSPECTION_SQL = """
SELECT c.column_name, c.data_type, c.character_maximum_length, c.is_nullable,
       c.column_default IS NOT NULL AS has_default, a.atttypmod, et.typname AS element_type
FROM information_schema.columns c
JOIN pg_catalog.pg_attribute a
  ON a.attrelid = to_regclass(format('%I.%I', c.table_schema, c.table_name))
 AND a.attname = c.column_name AND a.attnum > 0 AND NOT a.attisdropped
JOIN pg_catalog.pg_type t ON t.oid = a.atttypid
LEFT JOIN pg_catalog.pg_type et ON et.oid = t.typelem AND t.typcategory = 'A'
WHERE c.table_schema = 'public' AND c.table_name = :table
ORDER BY c.ordinal_position
"""


def target_column(row) -> TargetColumn:
    if row["data_type"] == "ARRAY":
        base = ELEMENT_TYPES.get(row["element_type"], "array of " + str(row["element_type"]))
        modifier = row["atttypmod"]
        length = modifier - 4 if base == "character varying" and modifier >= 4 else None
        array = True
    else:
        base, length, array = row["data_type"], row["character_maximum_length"], False
    return TargetColumn(
        row["column_name"],
        base,
        array,
        length,
        row["is_nullable"] == "NO",
        bool(row["has_default"]),
    )


async def introspect_table(connection, table: str) -> dict[str, TargetColumn]:
    """One information_schema/pg_attribute query for one table."""
    from sqlalchemy import text

    result = await connection.execute(text(INTROSPECTION_SQL), {"table": table})
    return {row["column_name"]: target_column(row) for row in result.mappings().all()}


async def introspect_target(connection, definitions: list[dict]) -> dict:
    tables = [c["name"] for c in definitions if c["type"] != "view"]
    return {name: await introspect_table(connection, name) for name in tables}


def target_spec_json(spec: dict) -> dict:
    """Schema-only description, safe to publish (no record value)."""
    return {
        "tables": {
            table: [asdict(column) for column in columns.values()]
            for table, columns in sorted(spec.items())
        }
    }


def target_spec_from_json(value: dict) -> dict:
    spec = None
    try:
        spec = {
            table: {column["name"]: TargetColumn(**column) for column in columns}
            for table, columns in value["tables"].items()
        }
    except (KeyError, TypeError, AttributeError):
        pass
    if spec is None:
        raise ImportBlocked("target_spec_invalid")
    return spec


def _describe(column: TargetColumn) -> str:
    base = column.type + (f"({column.length})" if column.length is not None else "")
    return base + ("[]" if column.array else "")


def _expected(step: Step) -> str:
    return "|".join(kind + ("[]" if step.array else "") for kind in sorted(step.types))


def preflight(definitions: list[dict], spec: dict, row_counts: dict) -> dict:
    """Every exported field needs a target column of its expected type and shape.

    An unbounded relation is a warning while its source table is empty
    (D-0019), a blocker otherwise: ``unbounded_relation_target_scalar`` over a
    scalar column, ``unbounded_relation_arrays_not_enabled`` over an array
    column while UNBOUNDED_RELATION_ARRAYS is off. A relation definition
    refused by ``relation_definition_problem`` follows the same rule with its
    own code, whatever its column. Unsupplied NOT NULL columns without default
    are blockers; other unsupplied columns use their default.
    """
    blockers, warnings, defaulted, checked = [], [], [], 0
    by_rows = (UNBOUNDED_SCALAR, UNBOUNDED_NOT_ENABLED, *RELATION_DEFINITION_CODES)
    for collection in definitions:
        if collection["type"] == "view":
            continue
        name = collection["name"]
        columns = spec.get(name)
        if columns is None:
            blockers.append({"code": "target_table_missing", "collection": name})
            continue
        plan = RowPlan(collection)
        for step in plan.steps:
            checked += 1
            column = columns.get(step.column)
            problem = None
            if step.refusal is not None:
                problem = step.refusal
            elif column is None:
                problem = "target_column_missing"
            elif column.type not in KNOWN_TARGET_TYPES:
                problem = "unsupported_target_column_type"
            elif column.array != step.array:
                problem = UNBOUNDED_SCALAR if step.unbounded else "target_column_shape_mismatch"
            elif column.type not in step.types:
                problem = "target_column_type_mismatch"
            elif step.unbounded and not UNBOUNDED_RELATION_ARRAYS:
                problem = UNBOUNDED_NOT_ENABLED
            if problem is None:
                continue
            entry = {
                "code": problem,
                "collection": name,
                "field": step.column,
                "expected": _expected(step),
                "actual": _describe(column) if column else None,
            }
            if problem in by_rows and not row_counts.get(name):
                warnings.append(entry)
            else:
                blockers.append(entry)
        for column in columns.values():
            if column.name in plan.columns:
                continue
            if column.not_null and not column.has_default:
                blockers.append(
                    {
                        "code": "target_required_column_not_supplied",
                        "collection": name,
                        "field": column.name,
                    }
                )
            else:
                defaulted.append({"collection": name, "field": column.name})
    return {
        "blockers": blockers,
        "warnings": warnings,
        "defaulted_target_columns": defaulted,
        "columns_checked": checked,
    }


def _scalar_checker(column: TargetColumn):
    """Compiled check of one non-NULL scalar (or array element) for a column."""
    kind, length = column.type, column.length
    if kind in STRING_TYPES:

        def check(value):
            if not isinstance(value, str):
                raise ImportBlocked("invalid_text")
            _check_pg_string(value, "nul_in_postgresql_text")
            if length is not None and len(value) > length:
                # varchar(n) counts characters (code points of valid UTF-8).
                # PostgreSQL would even truncate excess trailing spaces silently.
                raise ImportBlocked("value_exceeds_target_length")

    elif kind == "double precision":

        def check(value):
            if not isinstance(value, float) or not math.isfinite(value):
                raise ImportBlocked("invalid_number")

    elif kind in INTEGER_RANGES:
        low, high = INTEGER_RANGES[kind]

        def check(value):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ImportBlocked("invalid_number")
            if not low <= value < high:
                raise ImportBlocked("integer_outside_target_range")

    elif kind == "boolean":

        def check(value):
            if not isinstance(value, bool):
                raise ImportBlocked("invalid_boolean")

    elif kind == "timestamp with time zone":

        def check(value):
            if not isinstance(value, datetime) or value.tzinfo is None:
                raise ImportBlocked("invalid_timestamp")

    elif kind == "jsonb":

        def check(value):
            # Text produced by json_text(), already checked by check_json_text().
            if not isinstance(value, str):
                raise ImportBlocked("invalid_json")

    else:

        def check(value):
            raise ImportBlocked("unsupported_target_column_type")

    return check


def value_checker(column: TargetColumn):
    """Compiled check of converted values against one PostgreSQL column.

    Shape, NOT NULL, type, length and representability; never coerces.
    """
    scalar, not_null = _scalar_checker(column), column.not_null
    if column.array:

        def check(value):
            if value is None:
                if not_null:
                    raise ImportBlocked("null_in_not_null_column")
                return
            if not isinstance(value, list):
                raise ImportBlocked("scalar_value_for_array_column")
            for item in value:
                scalar(item)

    else:

        def check(value):
            if value is None:
                if not_null:
                    raise ImportBlocked("null_in_not_null_column")
                return
            if isinstance(value, list):
                raise ImportBlocked("array_value_for_scalar_column")
            scalar(value)

    return check


def check_value(column: TargetColumn, value) -> None:
    """Check one converted value against its PostgreSQL column; never coerce."""
    value_checker(column)(value)


# ---------------------------------------------------------------------------
# Exact row keys and table fingerprints (source mapped vs target read).
# ---------------------------------------------------------------------------


def decimal_key(number: Decimal) -> str:
    """Exact, context-free key of a JSON number as jsonb stores it.

    The value, normalized, plus its display scale: PostgreSQL numeric_in keeps
    dscale = max(0, fraction digits - exponent), which is max(0, -exponent) of
    the parsed Decimal, and numeric_out prints exactly that many decimals. So
    1e2 and 100 (dscale 0) are the same jsonb number, as are 1.5E-1 and 0.15
    (dscale 2), but 1.0 (1) is not 1 (0) and 2.50 (2) is not 2.5 (1): PPBase
    serves 1.0 as a float and 1 as an int to a typed client.
    """
    sign, digits, exponent = number.as_tuple()
    scale = max(-exponent, 0)
    digits = list(digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    while len(digits) > 1 and digits[0] == 0:
        digits.pop(0)
    if digits == [0]:
        return f"0/{scale}"
    return ("-" if sign else "") + "".join(map(str, digits)) + f"e{exponent}/{scale}"


def _canonical_json_value(value) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, Decimal):
        return decimal_key(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ",".join(_canonical_json_value(item) for item in value) + "]"
    return (
        "{"
        + ",".join(
            json.dumps(key, ensure_ascii=False) + ":" + _canonical_json_value(item)
            for key, item in sorted(value.items())
        )
        + "}"
    )


_CANONICAL_JSON_CACHE = {"{}": "{}", "null": "null"}


def canonical_json(text: str) -> str:
    """Exact jsonb value identity: key order, spacing and 1e2/100 aside.

    Every string, key, literal and number (value and scale, ``decimal_key``)
    counts; only what jsonb itself does not store is ignored.
    """
    cached = _CANONICAL_JSON_CACHE.get(text)
    if cached is not None:
        return cached
    return _canonical_json_value(_parse_exact_json(text))


def row_key(values, json_positions=()) -> str:
    """Exact typed key of one row as stored or read back.

    repr() keeps every character and distinguishes str/int/float/bool/None,
    -0.0 from 0.0 and microseconds; JSON columns compare canonically.
    """
    if json_positions:
        values = list(values)
        for position in json_positions:
            value = values[position]
            if isinstance(value, str):
                values[position] = canonical_json(value)
    return repr(tuple(values))


def row_digest(key: str) -> int:
    return int.from_bytes(hashlib.sha256(key.encode("utf-8", "surrogatepass")).digest(), "big")


class Fingerprint:
    """Order-independent multiset digest: row count and sum of row SHA-256."""

    def __init__(self):
        self.rows = 0
        self.total = 0

    def add(self, value: int) -> None:
        self.rows += 1
        self.total = (self.total + value) % MULTISET_MODULUS

    def hexdigest(self) -> str:
        return f"{self.rows}:{self.total:064x}"


# ---------------------------------------------------------------------------
# Offline diagnosis.
# ---------------------------------------------------------------------------


class SourceAnomalies:
    """Orphan relations and absent files: preserved as-is, reported, counted."""

    def __init__(self, details: bool):
        self.relations: dict[tuple, dict] = {}
        self.files: dict[tuple, dict] = {}
        self.relation_records = 0
        self.file_records = 0
        self.private = {"dangling_relations": [], "missing_files": []} if details else None

    def relation(self, collection, field, target, record, missing):
        entry = self.relations.setdefault(
            (collection, field),
            {"target": target, "references": 0, "records": 0, "targets": set()},
        )
        entry["references"] += len(missing)
        entry["records"] += 1
        entry["targets"].update(missing)
        if self.private is not None:
            self.private["dangling_relations"].extend(
                [collection, field, record, reference] for reference in missing
            )

    def file(self, collection, field, keys):
        entry = self.files.setdefault(
            (collection, field), {"references": 0, "records": 0, "files": set()}
        )
        entry["references"] += len(keys)
        entry["records"] += 1
        entry["files"].update(keys)
        if self.private is not None:
            self.private["missing_files"].extend(keys)

    def report(self) -> dict:
        relations = [
            {
                "collection": collection,
                "field": field,
                "target": entry["target"],
                "references": entry["references"],
                "records": entry["records"],
                "distinct_targets": len(entry["targets"]),
            }
            for (collection, field), entry in self.relations.items()
        ]
        files = [
            {
                "collection": collection,
                "field": field,
                "references": entry["references"],
                "records": entry["records"],
                "distinct_files": len(entry["files"]),
            }
            for (collection, field), entry in self.files.items()
        ]
        return anomaly_summary(relations, files, self.relation_records, self.file_records)


def anomaly_summary(relations, files, relation_records, file_records) -> dict:
    return {
        "policy": ANOMALY_POLICY,
        "blocking": False,
        "dangling_relations": {
            "references": sum(item["references"] for item in relations),
            "records": relation_records,
            "fields": relations,
        },
        "missing_files": {
            "references": sum(item["references"] for item in files),
            "records": file_records,
            "distinct_files": sum(item["distinct_files"] for item in files),
            "fields": files,
        },
    }


def _add_blocker(blockers: list, code: str, **context) -> None:
    blockers.append({"code": code, **context})


def issue_key(column, code, arrays_not_enabled, scalar_targets) -> tuple:
    """(column, code) counted for one problem of one diagnosed row.

    An unbounded relation keeps one code per cause: the importer flag over an
    array column (``arrays_not_enabled``), the scalar target over a scalar
    column (``scalar_targets``, whose shape refusal is that D-0019 blocker).
    """
    if code == UNBOUNDED_SCALAR and column in arrays_not_enabled:
        return column, UNBOUNDED_NOT_ENABLED
    if code == "array_value_for_scalar_column" and column in scalar_targets:
        return column, UNBOUNDED_SCALAR
    return column, code


def _entry_key(entry) -> tuple:
    if isinstance(entry, dict):
        return entry.get("code"), entry.get("collection"), entry.get("field")
    return entry, None, None


def merge_entries(entries: list) -> list:
    """One entry per (code, collection, field), details merged in order.

    The preflight of a target column and the rows it blocks describe the same
    problem (e.g. an unbounded relation over a scalar column): one blocker
    carries both the expected/actual column and the affected rows.
    """
    merged: dict[tuple, object] = {}
    for entry in entries:
        key = _entry_key(entry)
        if key in merged and isinstance(entry, dict):
            merged[key] = {**merged[key], **entry}
        else:
            merged.setdefault(key, entry)
    return list(merged.values())


def diagnose(
    data: Path,
    schema: Path,
    *,
    source_version: str | None = None,
    manifest: Path | None = None,
    target_columns: dict | None = None,
    private: dict | None = None,
) -> dict:
    """Read-only diagnosis of a closed snapshot.

    Orphan relations and absent referenced files are preserved anomalies, not
    blockers. With ``target_columns`` (see ``target_spec_from_json``) every
    value is also checked against its PostgreSQL column. Differences that a
    client can observe between what PocketBase 0.22.34 serves and what the
    imported value will be served as are explicit warnings with affected rows
    per field, the stored value being imported as is:
    ``jsonb_key_order_normalized`` (keys served in jsonb order),
    ``jsonb_number_notation_normalized`` (1e2 served as 100),
    ``sql_null_json_stored_as_jsonb_null`` (PB SQL NULL stored as JSON null),
    ``list_served_deduplicated_by_pocketbase`` (PB drops "" and duplicates of
    a stored list) and ``single_value_served_as_last_element`` (PB serves a
    single value stored as a JSON array by its last element). A duplicate JSON
    key, whose earlier values jsonb would drop, blocks
    (``jsonb_duplicate_keys_collapsed``), as does a json text that PocketBase
    serves as a string (``json_text_served_as_string_by_pocketbase``).
    ``private`` receives record-level details (IDs, references, file keys)
    for a private report.
    """
    started = time.monotonic()
    definitions = load_schema(schema)
    report = {
        "mode": "offline_read_only",
        "source_version": source_version,
        "production_authorized": False,
        "schema_sha256": digest(schema),
        "definitions_sha256": definitions_fingerprint(definitions),
        "blockers": [],
        "collections": [],
        "warnings": [],
    }
    blockers = report["blockers"]
    version_code, evidence = source_version_status(source_version)
    report["source_version_validated"] = version_code is None
    report["source_version_evidence"] = evidence
    report["validated_source_versions"] = sorted(VALIDATED_SOURCE_VERSIONS)
    if version_code:
        _add_blocker(blockers, version_code)
    start_hash = digest(data / "data.db")
    files, storage_fields = storage_source(data, manifest)
    report.update(storage_fields)
    report["file_bytes_copied"] = False
    spec = target_columns
    report["target_types_checked"] = spec is not None
    anomalies = SourceAnomalies(details=private is not None)
    referenced = set()
    relation_count = 0
    blocked_rows = 0
    by_id = {c["id"]: c for c in definitions}
    with snapshot(data) as db:
        if [row[0] for row in db.execute("PRAGMA integrity_check")] != ["ok"]:
            _add_blocker(blockers, "sqlite_integrity_failure")
        actual = {row["name"]: dict(row) for row in db.execute("SELECT * FROM _collections")}
        expected = {c["name"]: c for c in definitions}
        for name in sorted(set(actual) - set(expected)):
            _add_blocker(blockers, "unexpected_collection", collection=name)
        if spec is not None:
            counts = {}
            for c in definitions:
                if c["type"] != "view" and c["name"] in actual:
                    try:
                        counts[c["name"]] = db.execute(
                            f"SELECT COUNT(*) FROM {quote(c['name'])}"
                        ).fetchone()[0]
                    except sqlite3.Error:
                        counts[c["name"]] = 0
            checked = preflight(definitions, spec, counts)
            blockers.extend(checked["blockers"])
            report["warnings"].extend(checked["warnings"])
            report["target_preflight"] = {
                "columns_checked": checked["columns_checked"],
                "defaulted_target_columns": checked["defaulted_target_columns"],
            }
        id_sets: dict[str, set] = {}

        def target_ids(name: str) -> set:
            if name not in id_sets:
                id_sets[name] = {row[0] for row in db.execute(f"SELECT id FROM {quote(name)}")}
            return id_sets[name]

        for collection in definitions:
            name = collection["name"]
            current = actual.get(name)
            if not current:
                _add_blocker(blockers, "missing_collection", collection=name)
                continue
            for key in (
                "id",
                "type",
                "system",
                "schema",
                "indexes",
                "options",
                "listRule",
                "viewRule",
                "createRule",
                "updateRule",
                "deleteRule",
            ):
                value = (
                    strict_json(current[key])
                    if key in {"schema", "indexes", "options"}
                    else current[key]
                )
                wanted = collection[key]
                # PB regenerates view field IDs when importing a schema. Their
                # SQL and field contracts are still compared; no data is copied.
                if key == "schema" and collection["type"] == "view":
                    value = [{k: v for k, v in f.items() if k != "id"} for f in value]
                    wanted = [{k: v for k, v in f.items() if k != "id"} for f in wanted]
                if value != wanted:
                    _add_blocker(blockers, "schema_drift", collection=name, field=key)
            if collection["type"] == "view":
                continue
            table = db.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
            if not table:
                _add_blocker(blockers, "missing_table", collection=name)
                continue
            columns = {
                row["name"].lower() for row in db.execute(f"PRAGMA table_info({quote(name)})")
            }
            if "WITHOUT ROWID" in table[0].upper() or columns & {"rowid", "_rowid_", "oid"}:
                _add_blocker(blockers, "ambiguous_sqlite_rowid", collection=name)
                continue
            table_spec = spec.get(name) if spec is not None else None
            # Unbounded relations whose target column already is an array: the
            # cause of the refusal is this importer's flag, not the target.
            arrays_not_enabled = {
                field["name"]
                for field in collection["schema"]
                if relation_unbounded(field)
                and not UNBOUNDED_RELATION_ARRAYS
                and table_spec
                and table_spec.get(field["name"])
                and table_spec[field["name"]].array
            }
            # Unbounded relations over a scalar target column (a database not
            # converted by 1789236850): the shape refusal of each row is the
            # D-0019 blocker of the preflight, reported once with its rows.
            scalar_targets = {
                field["name"]
                for field in collection["schema"]
                if relation_unbounded(field)
                and table_spec
                and table_spec.get(field["name"])
                and not table_spec[field["name"]].array
            }
            for field in collection["schema"]:
                code = relation_definition_problem(field)
                if code is None and relation_unbounded(field) and not UNBOUNDED_RELATION_ARRAYS:
                    code = (
                        UNBOUNDED_NOT_ENABLED
                        if field["name"] in arrays_not_enabled
                        else UNBOUNDED_SCALAR
                    )
                if code is not None:
                    # Kept for empty tables; merged away when a blocker exists.
                    report["warnings"].append(
                        {"code": code, "collection": name, "field": field["name"]}
                    )
            plan = RowPlan(collection)
            column_specs = (
                [table_spec.get(column) for column in plan.columns] if table_spec else None
            )
            column_checks = (
                [(value_checker(c), c.name) if c else None for c in column_specs]
                if column_specs
                else None
            )
            relation_fields = [
                (
                    field["name"],
                    source_multiple(field),
                    by_id.get(field["options"].get("collectionId")),
                )
                for field in collection["schema"]
                if field["type"] == "relation"
            ]
            file_fields = [
                (field["name"], source_multiple(field))
                for field in collection["schema"]
                if field["type"] == "file"
            ]
            json_positions = [
                (position, step.column)
                for position, step in enumerate(plan.steps)
                if step.convert is json_text
            ]
            list_positions = [
                (plan.columns.index(field["name"]), field["name"], source_multiple(field))
                for field in collection["schema"]
                if field["type"] in LIST_KINDS
            ]
            count, low, high = 0, None, None
            ids_hash, rows_hash = hashlib.sha256(), hashlib.sha256()
            issue_counts = Counter()
            served_counts = Counter()
            for raw in source_rows(db, collection):
                row = dict(raw)
                count += 1
                rowid = row["__c11_rowid"]
                low, high = rowid if low is None else min(low, rowid), rowid
                ids_hash.update((str(rowid) + ":" + str(row.get("id")) + "\n").encode())
                rows_hash.update(
                    json.dumps(row, sort_keys=True, ensure_ascii=False).encode() + b"\n"
                )
                problems = []
                try:
                    values, archive = plan.convert(row, problems)
                except KeyError:
                    problems.append((None, "missing_source_column"))
                    values = None
                if values is not None and column_checks is not None:
                    for compiled, value in zip(column_checks, values, strict=True):
                        if compiled is None or value is _FAILED:
                            continue
                        try:
                            compiled[0](value)
                        except ImportBlocked as exc:
                            problems.append((compiled[1], exc.code))
                if values is not None:
                    try:
                        auth_legacy_text(archive)  # the same check as before COPY
                    except ImportBlocked as exc:
                        problems.append(("auth_legacy", exc.code))
                    for position, column in json_positions:
                        if values[position] is _FAILED:
                            continue
                        if row[column] is None:
                            # PB 0.22 serves SQL NULL as null and matches it with
                            # `field = null`; the NOT NULL jsonb target can only
                            # store JSON null, which PPBase's filter does not match.
                            served_counts[(column, SQL_NULL_JSON)] += 1
                        else:
                            for change in jsonb_text_changes(values[position]):
                                served_counts[(column, change)] += 1
                    for position, column, multiple in list_positions:
                        value = values[position]
                        if value is _FAILED:
                            continue
                        if multiple:
                            if pocketbase_list_changed(value):
                                served_counts[(column, LIST_DEDUPLICATED)] += 1
                        elif pocketbase_single_value(value) != value:
                            served_counts[(column, SINGLE_LAST_ELEMENT)] += 1
                record = row.get("id")
                record_ok = isinstance(record, str) and bool(record)
                dangling_row = False
                for field_name, multiple, target in relation_fields:
                    references = stored_references(row.get(field_name), multiple)
                    if not references:
                        continue
                    relation_count += len(references)
                    if target is None:
                        problems.append((field_name, "relation_target_collection_missing"))
                        continue
                    known = target_ids(target["name"])
                    missing = [item for item in references if item not in known]
                    if missing:
                        dangling_row = True
                        anomalies.relation(name, field_name, target["name"], record, missing)
                anomalies.relation_records += dangling_row
                missing_row = False
                for field_name, multiple in file_fields if record_ok else ():
                    keys = []
                    for filename in stored_references(row.get(field_name), multiple):
                        try:
                            key = file_key(collection["id"], record, filename)
                        except ImportBlocked as exc:
                            problems.append((field_name, exc.code))
                            continue
                        referenced.add(key)
                        if key not in files:
                            keys.append(key)
                    if keys:
                        missing_row = True
                        anomalies.file(name, field_name, keys)
                anomalies.file_records += missing_row
                if problems:
                    blocked_rows += 1
                    issue_counts.update(
                        {
                            issue_key(column, code, arrays_not_enabled, scalar_targets)
                            for column, code in problems
                        }
                    )
            for (column, code), issues in sorted(
                issue_counts.items(), key=lambda item: (item[0][1], str(item[0][0]))
            ):
                entry = {"code": code, "collection": name}
                if column is not None:
                    entry["field"] = column
                entry["affected_rows"] = issues
                blockers.append(entry)
            for (column, code), affected in sorted(served_counts.items()):
                report["warnings"].append(
                    {"code": code, "collection": name, "field": column, "affected_rows": affected}
                )
            report["collections"].append(
                {
                    "name": name,
                    "id": collection["id"],
                    "rows": count,
                    "rowid_min": low,
                    "rowid_max": high,
                    "identity_sha256": ids_hash.hexdigest(),
                    "source_rows_sha256": rows_hash.hexdigest(),
                }
            )
        # Only the frozen source index can reject PocketBase-accepted history;
        # duplicate mobile_message_id rows are legacy data to keep (D-0022).
        duplicates = idempotency_key_conflicts(db)
        if duplicates:
            _add_blocker(
                blockers, "duplicate_idempotency_key", field="system_id", groups=duplicates
            )
        internal = {
            r["name"]: db.execute(f"SELECT COUNT(*) FROM {quote(r['name'])}").fetchone()[0]
            for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            if r["name"] not in expected
        }
        report["internal_tables_not_imported"] = internal
        if any(
            count
            for name, count in internal.items()
            if name not in {"_collections", "_migrations", "sqlite_sequence"}
        ):
            report["warnings"].append("internal_records_require_separate_review")
    if digest(data / "data.db") != start_hash:
        raise ImportBlocked("snapshot_changed_during_diagnosis")
    unreferenced = sorted(set(files) - referenced)
    report["preserved_source_anomalies"] = anomalies.report()
    report["blockers"] = blockers = merge_entries(blockers)
    blocker_keys = {_entry_key(entry) for entry in blockers}
    report["warnings"] = [
        entry
        for entry in merge_entries(report["warnings"])
        if _entry_key(entry) not in blocker_keys
    ]
    if private is not None:
        private.update(anomalies.private)
        private["unreferenced_files"] = unreferenced
    report.update(
        {
            "data_db_sha256": start_hash,
            "files": len(files),
            "file_bytes": sum(f["bytes"] for f in files.values()),
            "file_manifest_sha256": inventory_fingerprint(files),
            "referenced_files": len(referenced),
            "present_referenced_files": len(referenced & set(files)),
            "unreferenced_files": len(unreferenced),
            "relations_checked": relation_count,
            "rows": sum(c["rows"] for c in report["collections"]),
            "blocked_rows": blocked_rows,
            "ready_for_isolated_rehearsal": not blockers,
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "peak_rss_kib": peak_rss_kib(),
        }
    )
    return report


# ---------------------------------------------------------------------------
# Private staging.
# ---------------------------------------------------------------------------


def stage_snapshot(data: Path, destination: Path, report: dict, *, manifest: Path | None = None):
    """Private verified copy; destination must be new and unpublished.

    Directory mode copies data.db and every storage object byte for byte
    (every directory 0700, every file 0600). Manifest mode copies data.db
    only, checked against the manifest, and records that no file byte was
    copied. Returns the storage inventory. Any failure raised here removes the
    destination. A process ended without Python unwinding cannot clean up:
    SIGKILL, the OOM killer, and by default SIGTERM and SIGHUP (a timeout,
    ``docker stop``, a closed terminal), which Python does not turn into an
    exception (see ``cancelled_by_termination``). The private copy of data.db
    (and storage/) then stays at ``destination``, and a retry with the same
    path fails with FileExistsError. Remove it, or use a new path.
    """
    if not report["ready_for_isolated_rehearsal"]:
        raise ImportBlocked("diagnostic_blockers")
    mode = report.get("storage_mode", "directory")
    if (mode == "manifest") != (manifest is not None):
        raise ImportBlocked("storage_mode_mismatch")
    destination.mkdir(mode=0o700, exist_ok=False)
    try:
        with snapshot(data):
            if manifest is None:
                before = storage_inventory(data / "storage")
            else:
                if path_present(data / "storage"):
                    raise ImportBlocked("storage_directory_and_manifest_both_supplied")
                checked = read_manifest(manifest, data / "data.db")
                if checked["summary"]["sha256"] != report["manifest"]["sha256"]:
                    raise ImportBlocked("manifest_changed_after_diagnosis")
                before = checked["storage"]
            if inventory_fingerprint(before) != report["file_manifest_sha256"]:
                raise ImportBlocked("storage_changed_after_diagnosis")
            shutil.copyfile(data / "data.db", destination / "data.db")
            (destination / "data.db").chmod(0o600)
            if digest(destination / "data.db") != report["data_db_sha256"]:
                raise ImportBlocked("snapshot_changed_after_diagnosis")
            if manifest is None:
                root = destination / "storage"
                root.mkdir(mode=0o700)
                created = set()
                for relative in before:
                    # One level at a time: mkdir(parents=True) would give the
                    # intermediate directories the default mode, not 0700.
                    parent = root
                    for part in PurePosixPath(relative).parts[:-1]:
                        parent = parent / part
                        if parent not in created:
                            parent.mkdir(mode=0o700, exist_ok=True)
                            created.add(parent)
                    target = root / relative
                    shutil.copyfile(data / "storage" / relative, target)
                    target.chmod(0o600)
                if storage_inventory(root) != before:
                    raise ImportBlocked("storage_copy_mismatch")
                if storage_inventory(data / "storage") != before:
                    raise ImportBlocked("storage_changed_during_copy")
            write_json_new(
                destination / "manifest.json",
                {
                    "diagnostic": report,
                    "storage": before,
                    "storage_mode": mode,
                    "file_bytes_copied": manifest is None,
                },
                ascii=True,  # file names may carry undecodable bytes
            )
            return before
    except BaseException:
        # Only the fresh directory exclusively created by this invocation.
        shutil.rmtree(destination)
        raise


# ---------------------------------------------------------------------------
# Isolated load: one caller transaction, COPY batches, exact verification.
# ---------------------------------------------------------------------------


class _Verification:
    """Evidence of one table, or of the rowid archive, during one load.

    ``source``: multiset digest of the converted source rows, taken when each
    batch is prepared. ``server_rows``/``server_hash``: count and exact sum of
    PostgreSQL's hash of each stored row, as returned by the batch readback.
    ``target``/``final_hash``: multiset digest and hash sum of the full typed
    re-read once every batch is in (``_reread``), compared by ``check_final``.
    """

    def __init__(self):
        self.source = Fingerprint()
        self.target = Fingerprint()
        self.server_rows = 0
        self.server_hash = 0
        self.final_hash = 0


class _LoadState:
    def __init__(self, driver, batch_size, fail_after):
        self.driver = driver
        self.batch_size = batch_size
        self.fail_after = fail_after
        self.count = 0
        self.prepared = 0
        self.statements = Counter()
        self.seconds = Counter()
        self.archive = _Verification()


ARCHIVE_SPEC = (
    TargetColumn("collection_id", "text", False, None, True, False),
    TargetColumn("record_id", "text", False, None, True, False),
    TargetColumn("source_rowid", "bigint", False, None, True, False),
    TargetColumn("auth_legacy", "jsonb", False, None, True, False),
)
# Readback order of the archive: key first, as for the record tables.
ARCHIVE_READ = ARCHIVE_SPEC[1:] + ARCHIVE_SPEC[:1]
ARCHIVE_JSON = (2,)
ROW_ALIAS = "c11_row"
# PostgreSQL's own hash of the complete stored row (every column, including
# defaulted ones), summed exactly: readback time versus end of the load.
ROW_HASH = f"hashtextextended({ROW_ALIAS}::text, 0)"


def _select_list(columns) -> str:
    return ",".join(
        f"{ROW_ALIAS}.{quote(column.name)}" + ("::text" if column.type == "jsonb" else "")
        for column in columns
    )


def _database_refusal(exc, collection, other="target_copy_failed") -> ImportBlocked:
    """Coded refusal of a PostgreSQL error: SQLSTATE and constraint name only.

    Class 23 is an integrity constraint (CHECK, UNIQUE, NOT NULL, FK...);
    anything else (trigger error, disk, cancel) keeps the ``other`` code. The
    driver error itself is dropped: its text carries the DETAIL of the row.
    """
    sqlstate = getattr(exc, "sqlstate", None)
    code = "target_constraint_violation" if str(sqlstate).startswith("23") else other
    return ImportBlocked(
        code,
        collection=collection,
        sqlstate=sqlstate,
        constraint=getattr(exc, "constraint_name", None),
    )


async def _copy(state, table, columns, records, collection):
    import asyncpg

    failure = None
    try:
        await state.driver.copy_records_to_table(
            table, records=records, columns=list(columns), schema_name="public"
        )
    except asyncpg.PostgresError as exc:
        failure = _database_refusal(exc, collection)
    except UnicodeEncodeError:
        # Unreachable after the value checks; still a code, never a crash.
        failure = ImportBlocked("invalid_unicode_for_postgresql", collection=collection)
    # Raised outside the handlers: neither __cause__ nor __context__ keeps the
    # driver error ("Failing row contains (...)" may hold password hashes).
    if failure is not None:
        raise failure
    state.statements["copy"] += 1


async def _immediate_constraints(state) -> None:
    """SET CONSTRAINTS ALL IMMEDIATE before the end-of-load verification.

    Deferred constraint checks and deferred constraint triggers would
    otherwise run at COMMIT, after every verification. Here they run now, in
    the verified window: a violation is a coded refusal (no DETAIL kept), and
    any row a deferred trigger could change is re-read afterwards.
    """
    import asyncpg

    failure = None
    try:
        await state.driver.execute("SET CONSTRAINTS ALL IMMEDIATE")
    except asyncpg.PostgresError as exc:
        failure = _database_refusal(
            exc, getattr(exc, "table_name", None), "target_deferred_check_failed"
        )
    if failure is not None:
        raise failure
    state.statements["other"] += 1


def _first_difference(source, values, json_positions, names):
    """Name of the first column whose read-back value differs, if any."""
    for index, name in enumerate(names):
        pair = (source[index], values[index])
        if index in json_positions:
            pair = tuple(canonical_json(v) if isinstance(v, str) else v for v in pair)
        if repr(pair[0]) != repr(pair[1]):
            return name
    return None


async def _verify_batch(state, sql, arguments, expected, json_positions, names, check, table):
    """Typed readback of one batch, compared value by value with the source.

    ``expected`` holds (key, converted source values, source row key) per
    copied row. The first selected column is the key, the last one
    PostgreSQL's hash of the complete stored row. Each returned row must be
    exactly one expected row, equal in every compared column (str/int/float/
    bool/None types, microseconds, -0.0, list order and JSON values). The
    hashes are summed for ``check_final``.
    """
    found = await state.driver.fetch(sql, *arguments)
    state.statements["readback"] += 1
    width = len(names)
    by_key = {}
    for record in found:
        if record[0] in by_key:
            raise ImportBlocked("target_readback_mismatch", collection=table)
        by_key[record[0]] = record
    if len(by_key) != len(expected):
        raise ImportBlocked("target_readback_mismatch", collection=table)
    for key, source, source_key in expected:
        record = by_key.get(key)
        if record is None:
            raise ImportBlocked("target_readback_mismatch", collection=table)
        values = tuple(record)
        if row_key(values[:width], json_positions) != source_key:
            field = _first_difference(source, values, json_positions, names)
            details = {} if field is None else {"field": field}
            raise ImportBlocked("target_readback_mismatch", collection=table, **details)
        check.server_rows += 1
        check.server_hash += values[width]


async def _reread(state, table, columns, json_positions, check) -> None:
    """Full typed re-read of one table once every batch is in.

    Independent of the batch comparison: another query (sequential scan, no
    key list), another time (after every table and the archive were written)
    and another accumulation. Fills the multiset digest of the row keys and
    the exact sum of PostgreSQL's row hashes of every stored row.
    """
    width = len(columns)
    sql = f"SELECT {_select_list(columns)},{ROW_HASH} FROM {quote(table)} {ROW_ALIAS}"
    async for record in state.driver.cursor(sql, prefetch=FINAL_PREFETCH):
        values = tuple(record)
        check.target.add(row_digest(row_key(values[:width], json_positions)))
        check.final_hash += values[width]
    state.statements["final_check"] += 1


def check_final(table: str, check: _Verification, expected_rows) -> None:
    """End-of-load verdict for one table; the first failing layer is raised.

    source rows = diagnosis; rows verified by the batch readback = rows re-read
    = source rows; hash sum of the stored rows unchanged since the readback;
    multiset digest of the re-read rows = that of the converted source rows.
    """
    if check.source.rows != expected_rows:
        raise ImportBlocked("source_row_count_mismatch", collection=table)
    if check.server_rows != check.source.rows or check.target.rows != check.source.rows:
        raise ImportBlocked("target_row_count_mismatch", collection=table)
    if check.final_hash != check.server_hash:
        raise ImportBlocked("target_changed_after_readback", collection=table)
    if check.target.hexdigest() != check.source.hexdigest():
        raise ImportBlocked("target_fingerprint_mismatch", collection=table)


class _Prepared:
    """One source batch, converted, checked and keyed, ready for COPY."""

    def __init__(self):
        self.batch, self.archive_batch = [], []
        self.expected, self.archive_expected = [], []


def json_columns(specs) -> tuple[int, ...]:
    return tuple(index for index, column in enumerate(specs) if column.type == "jsonb")


async def _load_table(state, db, collection, plan, specs, check, report_table):
    name = collection["name"]
    cursor = source_rows(db, collection)
    source_names = [column[0] for column in cursor.description]
    position = {column: index for index, column in enumerate(source_names)}
    if set(source_names) - plan.known:
        raise ImportBlocked("unmapped_source_columns", collection=name)
    if any(step.source not in position for step in plan.steps):
        raise ImportBlocked("missing_source_column", collection=name)
    steps = [(step.convert, position[step.source]) for step in plan.steps]
    rowid_at = position["__c11_rowid"]
    archive_at = [(key, position[key]) for key in AUTH_ARCHIVE_ORDER if key in position]
    checkers = [(value_checker(column), column.name) for column in specs]
    json_positions = json_columns(specs)
    readback = (
        f"SELECT {_select_list(specs)},{ROW_HASH} FROM {quote(name)} {ROW_ALIAS} "
        f"WHERE {ROW_ALIAS}.id = ANY($1::text[])"
    )
    archive_readback = (
        f"SELECT {_select_list(ARCHIVE_READ)},{ROW_HASH} FROM {quote(ARCHIVE_TABLE)} "
        f"{ROW_ALIAS} WHERE {ROW_ALIAS}.collection_id = $1 "
        f"AND {ROW_ALIAS}.record_id = ANY($2::text[])"
    )
    archive_names = tuple(column.name for column in ARCHIVE_READ)
    rows = iter(cursor)
    started = time.monotonic()

    def prepare():
        """Runs in a worker thread: read, convert, check and key one batch.

        The source digest is taken here, from the converted source values,
        apart from anything read back from PostgreSQL.
        """
        clock = time.perf_counter()
        out, limit = _Prepared(), state.batch_size
        if state.fail_after is not None:
            limit = max(min(limit, state.fail_after - state.prepared), 1)
        for raw in rows:
            # Every refusal below is raised outside its handler (ImportBlocked).
            values = failure = None
            try:
                values = [convert(raw[index]) for convert, index in steps]
            except (ImportBlocked, ValueError, TypeError, OverflowError, RecursionError):
                pass
            if values is None:
                # Locate the first failing column with the diagnosis code path.
                problems = []
                plan.convert(dict(zip(source_names, raw, strict=True)), problems)
                column, code = problems[0] if problems else (None, "unmappable_value")
                raise ImportBlocked(code, collection=name, field=column)
            for (checker, column), value in zip(checkers, values, strict=True):
                try:
                    checker(value)
                except ImportBlocked as exc:
                    failure = ImportBlocked(exc.code, collection=name, field=column)
                if failure is not None:
                    raise failure
            legacy = None
            try:
                legacy = auth_legacy_text({key: raw[index] for key, index in archive_at})
            except ImportBlocked as exc:
                failure = ImportBlocked(exc.code, collection=name, field="auth_legacy")
            if failure is not None:
                raise failure
            record, rowid = values[0], raw[rowid_at]
            source_key = row_key(values, json_positions)
            archived = (record, rowid, legacy, collection["id"])
            archive_key = row_key(archived, ARCHIVE_JSON)
            out.batch.append(tuple(values))
            out.archive_batch.append((collection["id"], record, rowid, legacy))
            out.expected.append((record, values, source_key))
            out.archive_expected.append((record, archived, archive_key))
            check.source.add(row_digest(source_key))
            state.archive.source.add(row_digest(archive_key))
            if len(out.batch) >= limit:
                break
        state.prepared += len(out.batch)
        state.seconds["prepare_in_worker"] += time.perf_counter() - clock
        return out if out.batch else None

    async def flush(prepared):
        clock = time.perf_counter()
        await _copy(state, name, plan.columns, prepared.batch, name)
        await _copy(state, ARCHIVE_TABLE, ARCHIVE_COLUMNS, prepared.archive_batch, name)
        copied = time.perf_counter()
        ids = [record[0] for record in prepared.batch]
        await _verify_batch(
            state, readback, [ids], prepared.expected, json_positions, plan.columns, check, name
        )
        await _verify_batch(
            state,
            archive_readback,
            [collection["id"], ids],
            prepared.archive_expected,
            ARCHIVE_JSON,
            archive_names,
            state.archive,
            name,
        )
        state.seconds["copy"] += copied - clock
        state.seconds["readback_and_compare"] += time.perf_counter() - copied
        state.count += len(prepared.batch)
        report_table["copy_batches"] += 1
        if state.fail_after is not None and state.count >= state.fail_after:
            raise ImportBlocked("injected_import_failure")

    # Batch N+1 is prepared while batch N is copied and read back. The worker
    # always finishes before this function returns, so the SQLite connection
    # is never used after the snapshot closes nor by two threads at once.
    pending = asyncio.ensure_future(asyncio.to_thread(prepare))
    try:
        while True:
            waited = time.perf_counter()
            prepared = await pending
            state.seconds["waiting_for_worker"] += time.perf_counter() - waited
            pending = None
            if prepared is None:
                break
            pending = asyncio.ensure_future(asyncio.to_thread(prepare))
            await flush(prepared)
    finally:
        if pending is not None:
            try:
                await pending
            except Exception:
                pass  # the primary failure is already propagating
    report_table["rows"] = check.source.rows
    report_table["seconds"] = round(time.monotonic() - started, 3)


async def target_anomalies(connection, definitions, spec, inventory, row_counts=None) -> dict:
    """Recount orphan relations and absent files in the loaded target tables.

    Same semantics as the diagnosis: a scalar reference is a non-empty value,
    an array reference is each stored element; nothing is filtered or fixed.
    One query per collection covers its scalar relations; array relations and
    file fields have their own queries. Collections known to be empty (their
    count is checked separately) are skipped.
    """
    from sqlalchemy import text

    by_id = {c["id"]: c for c in definitions}
    relations, files, referenced = [], [], set()
    relation_records = file_records = references_total = statements = 0
    for collection in definitions:
        if collection["type"] == "view":
            continue
        name = collection["name"]
        if row_counts is not None and not row_counts.get(name):
            continue
        columns = spec[name]
        source = f"{quote(name)} s"
        fields = [f for f in collection["schema"] if f["type"] == "relation"]
        scalar = [f for f in fields if not columns[f["name"]].array]
        arrays = [f for f in fields if columns[f["name"]].array]
        selects, joins, any_missing = [], [], []
        for index, field in enumerate(scalar):
            value, alias = "s." + quote(field["name"]), f"r{index}"
            target = by_id[field["options"]["collectionId"]]["name"]
            joins.append(f"LEFT JOIN {quote(target)} {alias} ON {alias}.id = {value}")
            present = f"{value} <> ''"
            missing = f"({present} AND {alias}.id IS NULL)"
            any_missing.append(missing)
            selects += [
                f"count(*) FILTER (WHERE {present})",
                f"count(*) FILTER (WHERE {missing})",
                f"count(DISTINCT {value}) FILTER (WHERE {missing})",
            ]
        for field in arrays:
            target = by_id[field["options"]["collectionId"]]["name"]
            any_missing.append(
                f"EXISTS (SELECT 1 FROM unnest(s.{quote(field['name'])}) e(ref) "
                f"LEFT JOIN {quote(target)} x ON x.id = e.ref WHERE x.id IS NULL)"
            )
        if any_missing:
            selects.append(f"count(*) FILTER (WHERE {' OR '.join(any_missing)})")
            result = (
                await connection.execute(
                    text(f"SELECT {', '.join(selects)} FROM {source} {' '.join(joins)}")
                )
            ).one()
            statements += 1
            relation_records += result[-1]
            for index, field in enumerate(scalar):
                total, orphans, targets = result[3 * index : 3 * index + 3]
                references_total += total
                if orphans:
                    relations.append(
                        {
                            "collection": name,
                            "field": field["name"],
                            "target": by_id[field["options"]["collectionId"]]["name"],
                            "references": orphans,
                            "records": orphans,  # one scalar value per record
                            "distinct_targets": targets,
                        }
                    )
        for field in arrays:
            target = by_id[field["options"]["collectionId"]]["name"]
            joined = (
                f"{source} CROSS JOIN LATERAL unnest(s.{quote(field['name'])}) e(ref) "
                f"LEFT JOIN {quote(target)} x ON x.id = e.ref"
            )
            total, orphans, records, targets = (
                await connection.execute(
                    text(
                        "SELECT count(*), count(*) FILTER (WHERE x.id IS NULL), "
                        "count(DISTINCT s.id) FILTER (WHERE x.id IS NULL), "
                        f"count(DISTINCT e.ref) FILTER (WHERE x.id IS NULL) FROM {joined}"
                    )
                )
            ).one()
            statements += 1
            references_total += total
            if orphans:
                relations.append(
                    {
                        "collection": name,
                        "field": field["name"],
                        "target": target,
                        "references": orphans,
                        "records": records,
                        "distinct_targets": targets,
                    }
                )
        missing_owners = set()
        for field in collection["schema"]:
            if field["type"] != "file":
                continue
            column, value = columns[field["name"]], "s." + quote(field["name"])
            if column.array:
                sql = f"SELECT s.id, e.ref FROM {source} CROSS JOIN LATERAL unnest({value}) e(ref)"
            else:
                sql = f"SELECT s.id, {value} FROM {source} WHERE {value} <> ''"
            keys, owners, count = set(), set(), 0
            for record, filename in (await connection.execute(text(sql))).all():
                key = f"{collection['id']}/{record}/{filename}"
                referenced.add(key)
                if key not in inventory:
                    keys.add(key)
                    owners.add(record)
                    count += 1
            statements += 1
            if count:
                missing_owners |= owners
                files.append(
                    {
                        "collection": name,
                        "field": field["name"],
                        "references": count,
                        "records": len(owners),
                        "distinct_files": len(keys),
                    }
                )
        file_records += len(missing_owners)
    summary = anomaly_summary(relations, files, relation_records, file_records)
    summary["relations_checked"] = references_total
    summary["unreferenced_files"] = len(set(inventory) - referenced)
    summary["statements"] = statements
    return summary


def _comparable(anomalies: dict) -> dict:
    return {
        "relations": sorted(
            (
                item["collection"],
                item["field"],
                item["references"],
                item["records"],
                item["distinct_targets"],
            )
            for item in anomalies["dangling_relations"]["fields"]
        ),
        "files": sorted(
            (
                item["collection"],
                item["field"],
                item["references"],
                item["records"],
                item["distinct_files"],
            )
            for item in anomalies["missing_files"]["fields"]
        ),
        "relation_records": anomalies["dangling_relations"]["records"],
        "file_records": anomalies["missing_files"]["records"],
    }


def check_conservation(recount: dict, diagnostic: dict) -> None:
    """The anomalies recounted in the target must equal the diagnosis."""
    preserved = diagnostic.get("preserved_source_anomalies")
    if (
        preserved is None
        or _comparable(recount) != _comparable(preserved)
        or recount["relations_checked"] != diagnostic.get("relations_checked")
        or recount["unreferenced_files"] != diagnostic.get("unreferenced_files")
    ):
        raise ImportBlocked("anomaly_conservation_mismatch")


TRIGGERS_SQL = """
SELECT c.relname AS table_name, t.tgname AS trigger_name, t.tgtype::int AS type,
       t.tgenabled::text AS enabled, pn.nspname || '.' || p.proname AS function,
       p.pronargs::int AS function_arguments, t.tgconstraint <> 0 AS is_constraint,
       t.tgdeferrable AS deferrable, t.tginitdeferred AS initially_deferred,
       t.tgnargs::int AS arguments, t.tgqual IS NOT NULL AS has_when,
       cardinality(t.tgattr::int2[]) > 0 AS has_columns
FROM pg_catalog.pg_trigger t
JOIN pg_catalog.pg_class c ON c.oid = t.tgrelid
JOIN pg_catalog.pg_proc p ON p.oid = t.tgfoid
JOIN pg_catalog.pg_namespace pn ON pn.oid = p.pronamespace
WHERE c.relnamespace = 'public'::regnamespace AND NOT t.tgisinternal
ORDER BY 1, 2
"""
EVENT_TRIGGERS_SQL = "SELECT count(*) FROM pg_catalog.pg_event_trigger WHERE evtenabled <> 'D'"


def check_target_triggers(found, event_triggers: int, tables) -> list[str]:
    """Refuse every trigger but the migrated rowid allocators, exactly defined.

    ``found`` holds the rows of TRIGGERS_SQL (every non-internal trigger of a
    public relation), ``event_triggers`` the number of enabled event triggers
    (database-wide: they would run inside this transaction on its DDL),
    ``tables`` the loaded table names. A trigger could rewrite rows at any
    time, a deferred constraint trigger even at COMMIT, after every check:
    ``unexpected_target_trigger``. An expected allocator that is absent is
    ``target_trigger_missing``. Returns the sorted 'table.trigger' names.
    """
    if event_triggers:
        raise ImportBlocked("unexpected_target_trigger", kind="event", triggers=event_triggers)
    expected = {(table, ROWID_TRIGGER) for table in ROWID_TABLES if table in tables}
    seen = set()
    for row in found:
        key = (row["table_name"], row["trigger_name"])
        definition = {name: row[name] for name in ROWID_TRIGGER_DEFINITION}
        if key not in expected or definition != ROWID_TRIGGER_DEFINITION:
            raise ImportBlocked(
                "unexpected_target_trigger", kind="table", collection=key[0], trigger=key[1]
            )
        seen.add(key)
    missing = sorted(expected - seen)
    if missing:
        raise ImportBlocked(
            "target_trigger_missing", collection=missing[0][0], trigger=ROWID_TRIGGER
        )
    return [f"{table}.{name}" for table, name in sorted(seen)]


async def target_triggers(connection, tables) -> list[str]:
    """Two catalog reads, then ``check_target_triggers``."""
    from sqlalchemy import text

    found = (await connection.execute(text(TRIGGERS_SQL))).mappings().all()
    events = (await connection.execute(text(EVENT_TRIGGERS_SQL))).scalar_one()
    return check_target_triggers(found, events, tables)


async def _final_verification(state, loaded, row_counts, total_rows) -> None:
    """Layer 2 of ``load_isolated``, once every batch is in.

    Deferred constraint work runs first (``_immediate_constraints``), so that
    nothing it changes escapes the re-read. Then each table, in load order,
    and the rowid archive are re-read in full and judged by ``check_final``;
    the loaded total and the archive must both equal the diagnosed total.
    """
    await _immediate_constraints(state)
    for entry, specs, check in loaded:
        name = entry["name"]
        await _reread(state, name, specs, json_columns(specs), check)
        check_final(name, check, row_counts.get(name))
        entry["source_fingerprint"] = check.source.hexdigest()
        entry["target_fingerprint"] = check.target.hexdigest()
    if state.count != total_rows:
        raise ImportBlocked("source_row_count_mismatch", scope="all_tables")
    await _reread(state, ARCHIVE_TABLE, ARCHIVE_READ, ARCHIVE_JSON, state.archive)
    check_final(ARCHIVE_TABLE, state.archive, total_rows)


def diagnosed_collections_match(definitions: list[dict], diagnostic: dict) -> bool:
    """The definitions handed to the load are exactly the diagnosed ones."""
    return definitions_fingerprint(definitions) == diagnostic.get("definitions_sha256") and [
        (c["name"], c["id"]) for c in definitions if c["type"] != "view"
    ] == [(c["name"], c["id"]) for c in diagnostic.get("collections", [])]


def stated_warnings(diagnostic: dict, codes) -> list[dict]:
    return [
        entry
        for entry in diagnostic.get("warnings", [])
        if isinstance(entry, dict) and entry.get("code") in codes
    ]


async def load_isolated(
    connection,
    data: Path,
    definitions: list[dict],
    *,
    fail_after: int | None = None,
    report: dict | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
):
    """Load on the caller's transaction; requires empty tables, never clears data.

    No hooks/repositories/HTTP writes: historical balances, authors, numbers and
    timestamps must not be recomputed. The caller owns the transaction
    (``engine.begin()``): checked before any SQL, then on the driver connection
    (an AUTOCOMMIT connection is refused). ``definitions`` must be exactly the
    diagnosed ones (``definitions_do_not_match_diagnosis``, before any SQL).
    The target may hold no trigger but the migrated rowid allocators, exactly
    defined, and no enabled event trigger (``check_target_triggers``: after the
    lock, after the rowid archive is created and at the end). Rows go through
    COPY batches on the caller's own connection, each value checked against
    its introspected column first. Verification layers, all exact:

    1. each batch is read back by key right after its COPY and compared value
       by value with the converted source (``target_readback_mismatch``);
    2. once everything is in, deferred constraints and deferred constraint
       triggers are made to run (SET CONSTRAINTS ALL IMMEDIATE: nothing is
       left for COMMIT), then each table and the rowid archive are re-read in
       full: the row count, the multiset digest of the re-read rows against
       the digest of the converted source rows taken at preparation, and the
       sum of PostgreSQL's hash of every stored row (all columns) against the
       sum seen at readback (``check_final``); the loaded total and the
       archive must equal the diagnosed total;
    3. orphan relations and absent files are recounted by SQL in the target
       and must equal the diagnosis (``check_conservation``).

    Returns the row count; ``report`` (optional dict) receives the aggregated
    evidence, including the served-value differences stated by the diagnosis.
    The caller's transaction is left with every deferrable constraint
    IMMEDIATE.
    """
    from sqlalchemy import text

    started, cpu_started = time.monotonic(), time.process_time()
    evidence = report if report is not None else {}
    staged = json.loads((data / "manifest.json").read_text())
    diagnostic = staged["diagnostic"]
    if not diagnostic["ready_for_isolated_rehearsal"]:
        raise ImportBlocked("diagnostic_blockers")
    if digest(data / "data.db") != diagnostic["data_db_sha256"]:
        raise ImportBlocked("staged_snapshot_changed")
    mode = staged.get("storage_mode", "directory")
    if mode == "directory":
        if storage_inventory(data / "storage") != staged["storage"]:
            raise ImportBlocked("staged_storage_changed")
    elif mode == "manifest":
        if path_present(data / "storage") or staged.get("file_bytes_copied"):
            raise ImportBlocked("storage_mode_mismatch")
        if inventory_fingerprint(staged["storage"]) != diagnostic["file_manifest_sha256"]:
            raise ImportBlocked("staged_storage_changed")
    else:
        raise ImportBlocked("storage_mode_mismatch")
    if not diagnosed_collections_match(definitions, diagnostic):
        # A collection left out of ``definitions`` would otherwise be skipped
        # silently while every verification of the other tables passes.
        raise ImportBlocked("definitions_do_not_match_diagnosis")
    # Before any statement: SQLAlchemy would otherwise autobegin a transaction
    # that nobody owns (and that is rolled back when the connection closes).
    if not connection.in_transaction():
        raise ImportBlocked("caller_transaction_required")
    current_db = (await connection.execute(text("SELECT current_database()"))).scalar_one()
    raw = await connection.get_raw_connection()
    driver = raw.driver_connection
    if not driver.is_in_transaction():
        # e.g. an AUTOCOMMIT isolation level: every statement would commit alone.
        raise ImportBlocked("caller_transaction_required")
    if current_db != "buma_c11_rehearsal":
        raise ImportBlocked("isolated_rehearsal_database_required")
    state = _LoadState(driver, batch_size, fail_after)
    state.statements["other"] += 1  # current_database()

    async def run(sql: str):
        state.statements["other"] += 1
        return await connection.execute(text(sql))

    tables = [c for c in definitions if c["type"] != "view"]
    names = {c["name"] for c in tables}
    # Lock the complete set before checking emptiness, so a concurrent importer
    # cannot pass the same precondition. PostgreSQL constraints remain enabled.
    await run(
        "LOCK TABLE " + ",".join(quote(c["name"]) for c in tables) + " IN ACCESS EXCLUSIVE MODE"
    )
    for c in tables:
        if (await run(f"SELECT EXISTS (SELECT 1 FROM {quote(c['name'])})")).scalar_one():
            raise ImportBlocked("target_not_empty")
    if (await run(f"SELECT to_regclass('{ARCHIVE_TABLE}')")).scalar_one():
        raise ImportBlocked("rowid_archive_exists")
    # The keyed readbacks are prepared once per table and their tables grow
    # from empty: a cached generic plan chosen on a small table would scan the
    # whole table at every batch. Plan each execution for its real arguments;
    # SET LOCAL ends with this transaction.
    await run("SET LOCAL plan_cache_mode = force_custom_plan")
    # Parallel plans allocate dynamic shared memory: in a Docker PostgreSQL with
    # the default 64 MB /dev/shm, the 16-join recount of a 317 066-row table
    # failed ("could not resize shared memory segment"). One serial session.
    await run("SET LOCAL max_parallel_workers_per_gather = 0")
    checks_started = time.monotonic()
    spec = await introspect_target(connection, definitions)
    state.statements["other"] += len(spec)
    row_counts = {c["name"]: c["rows"] for c in diagnostic["collections"]}
    checked = preflight(definitions, spec, row_counts)
    evidence.update(
        {
            "storage_mode": mode,
            "file_bytes_copied": mode == "directory",
            "batch_size": batch_size,
            "json_normalizations": stated_warnings(diagnostic, JSON_WARNING_CODES),
            "pocketbase_read_normalizations": stated_warnings(diagnostic, READ_NORMALIZATION_CODES),
            "preflight": {
                "tables": len(spec),
                "columns_checked": checked["columns_checked"],
                "blockers": checked["blockers"],
                "warnings": checked["warnings"],
                "defaulted_target_columns": checked["defaulted_target_columns"],
            },
        }
    )
    if checked["blockers"]:
        first = checked["blockers"][0]
        raise ImportBlocked(
            first["code"], collection=first.get("collection"), field=first.get("field")
        )
    triggers = await target_triggers(connection, names)
    state.statements["other"] += 2
    evidence["preflight"]["target_triggers"] = triggers
    await run(
        f"CREATE TABLE {ARCHIVE_TABLE} ("
        "collection_id text NOT NULL, record_id text NOT NULL, source_rowid bigint NOT NULL, "
        "auth_legacy jsonb NOT NULL, PRIMARY KEY(collection_id,record_id), "
        "UNIQUE(collection_id,source_rowid))"
    )
    # An event trigger could have attached a trigger to the new archive.
    if await target_triggers(connection, names) != triggers:
        raise ImportBlocked("unexpected_target_trigger", kind="changed")
    state.statements["other"] += 2
    tables_report, loaded = [], []
    evidence["tables"] = tables_report
    load_started = time.monotonic()
    with snapshot(data, worker_threads=True) as db:
        for c in tables:
            plan = RowPlan(c)
            specs = [spec[c["name"]][column] for column in plan.columns]
            entry = {"name": c["name"], "rows": 0, "copy_batches": 0}
            tables_report.append(entry)
            check = _Verification()
            loaded.append((entry, specs, check))
            await _load_table(state, db, c, plan, specs, check, entry)
    load_seconds = time.monotonic() - load_started
    final_started = time.monotonic()
    await _final_verification(state, loaded, row_counts, diagnostic["rows"])
    archive = state.archive
    final_seconds = time.monotonic() - final_started
    conservation_started = time.monotonic()
    recount = await target_anomalies(
        connection, definitions, spec, staged["storage"], row_counts=row_counts
    )
    state.statements["conservation"] += recount["statements"]
    check_conservation(recount, diagnostic)
    # Nothing may have added, changed or removed a trigger during the load.
    if await target_triggers(connection, names) != triggers:
        raise ImportBlocked("unexpected_target_trigger", kind="changed")
    state.statements["other"] += 2
    evidence.update(
        {
            "rows": state.count,
            "archive_rows": archive.target.rows,
            "archive_fingerprint": archive.target.hexdigest(),
            "typed_batch_readback": True,
            "deferred_constraints_run_before_final_reread": True,
            "final_typed_reread": True,
            "fingerprints_match": True,
            "server_row_hashes_unchanged_since_readback": True,
            "rows_equal_diagnosed_total": True,
            "triggers_unchanged_during_load": True,
            "conservation": {
                "dangling_relations": recount["dangling_relations"],
                "missing_files": recount["missing_files"],
                "relations_checked": recount["relations_checked"],
                "unreferenced_files": recount["unreferenced_files"],
                "matches_diagnostic": True,
            },
            "statements": dict(state.statements),
            "timings_seconds": {
                "checks": round(load_started - checks_started, 3),
                "copy_and_readback": round(load_seconds, 3),
                "copy_and_readback_breakdown": {
                    name: round(value, 3) for name, value in sorted(state.seconds.items())
                },
                "final_check": round(final_seconds, 3),
                "conservation": round(time.monotonic() - conservation_started, 3),
                "total": round(time.monotonic() - started, 3),
                "importer_cpu": round(time.process_time() - cpu_started, 3),
            },
            "peak_rss_kib": peak_rss_kib(),
        }
    )
    return state.count


def array_relation_fields(definitions: list[dict]):
    """(collection, field) of every relation stored as a list and imported."""
    for collection in definitions:
        if collection["type"] == "view":
            continue
        for field in collection["schema"]:
            if (
                field["type"] == "relation"
                and relation_definition_problem(field) is None
                and source_multiple(field)
            ):
                yield collection, field


def source_relation_array(db, collection: dict, field: dict) -> dict:
    """SQLite's own reading of one stored relation list, element by element.

    json_each, SQLite's JSON parser and not this importer's: rows, rows with
    elements, elements, longest element (characters), and the multiset digest
    of (record id, position, element), so that order and repeated elements
    count. A value that is no JSON list, or a non-text element, is ``invalid``
    (the diagnosis refuses those already).
    """
    table, column = quote(collection["name"]), quote(field["name"])
    # CASE fixes the evaluation order: json_type() fails on malformed JSON.
    valid = f"CASE WHEN json_valid(t.{column}) THEN json_type(t.{column}) = 'array' ELSE 0 END"
    rows, invalid = db.execute(
        f"SELECT count(*), count(*) FILTER (WHERE NOT {valid}) FROM {table} t"
    ).fetchone()
    found = Fingerprint()
    owners, longest = set(), 0
    for record, position, value, kind in db.execute(
        f"SELECT t.id, j.key, j.value, j.type FROM {table} t, "
        f"json_each(CASE WHEN {valid} THEN t.{column} ELSE '[]' END) j"
    ):
        if kind != "text":
            invalid += 1
        found.add(row_digest(repr((record, position, value))))
        owners.add(record)
        longest = max(longest, len(value) if isinstance(value, str) else 0)
    return {
        "rows": rows,
        "rows_with_elements": len(owners),
        "elements": found.rows,
        "longest_element": longest,
        "invalid": invalid,
        "digest": found.hexdigest(),
    }


async def target_relation_array(driver, collection: dict, field: dict) -> dict:
    """The same reading of the loaded array column: unnest WITH ORDINALITY."""
    table, column = quote(collection["name"]), quote(field["name"])
    rows, invalid = await driver.fetchrow(
        f"SELECT count(*), count(*) FILTER (WHERE {column} IS NULL) FROM {table}"
    )
    found = Fingerprint()
    owners, longest = set(), 0
    sql = (
        f"SELECT s.id, e.position - 1, e.ref FROM {table} s "
        f"CROSS JOIN LATERAL unnest(s.{column}) WITH ORDINALITY e(ref, position)"
    )
    async for record, position, value in driver.cursor(sql, prefetch=FINAL_PREFETCH):
        if not isinstance(value, str):
            invalid += 1
        found.add(row_digest(repr((record, position, value))))
        owners.add(record)
        longest = max(longest, len(value) if isinstance(value, str) else 0)
    return {
        "rows": rows,
        "rows_with_elements": len(owners),
        "elements": found.rows,
        "longest_element": longest,
        "invalid": invalid,
        "digest": found.hexdigest(),
    }


async def recount_relation_arrays(connection, data: Path, definitions: list[dict]) -> list[dict]:
    """Recount every array relation after the load, independently of its path.

    The load compares the target with this importer's own parsing of each
    stored list; here SQLite's JSON reader over the staged snapshot and
    PostgreSQL's unnest over the loaded columns must give the same rows,
    elements, positions and values (``source_relation_array``,
    ``target_relation_array``), else ``relation_array_recount_mismatch``. A
    scalar target column passes the preflight only for an empty table (a
    warning): its rows are counted, and any source row is the D-0019 blocker
    ``unbounded_relation_target_scalar``. Returns per-field counts and the
    target column type, never a value or a digest.
    """
    driver = (await connection.get_raw_connection()).driver_connection
    results, tables = [], {}
    with snapshot(data) as db:
        for collection, field in array_relation_fields(definitions):
            name = collection["name"]
            if name not in tables:
                tables[name] = await introspect_table(connection, name)
            column = tables[name].get(field["name"])
            source = source_relation_array(db, collection, field)
            context = {"collection": name, "field": field["name"]}
            entry = {**context, "target_column": _describe(column) if column else None}
            if column is not None and column.array:
                target = await target_relation_array(driver, collection, field)
                entry["sqlite_json_each_equals_postgresql_unnest"] = True
            else:
                if source["rows"]:
                    raise ImportBlocked(UNBOUNDED_SCALAR, **context)
                rows = await driver.fetchval(f"SELECT count(*) FROM {quote(name)}")
                target = {**source, "rows": rows}
                entry["empty_table_over_scalar_column"] = True
            if source["invalid"] or source != target:
                raise ImportBlocked(RELATION_ARRAY_RECOUNT, **context)
            for key in ("rows", "rows_with_elements", "elements", "longest_element"):
                entry[key] = source[key]
            results.append(entry)
    return results


async def import_snapshot(
    engine,
    data: Path,
    destination: Path,
    diagnostic: dict,
    definitions: list[dict],
    *,
    manifest: Path | None = None,
    fail_after: int | None = None,
    report: dict | None = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
    auth_fail_after: str | None = None,
):
    """Single entry point: records, accounts and settings in one transaction.

    Before anything is written or copied: the engine must hide bound values
    (``create_async_engine(..., hide_parameters=True)``, else
    ``engine_hide_parameters_required``) and the diagnosis must be ready
    (``diagnostic_blockers``, as ``stage_snapshot`` would refuse it), then
    ``check_ready`` of ``scripts.c11_auth_import`` runs on its own short
    read-only transaction (rehearsal database, source accounts and settings
    without blocker, empty ``_superusers``, no ``settings`` parameter, no auth
    archive, same auth collection IDs), so that an account or settings refusal
    never comes after the whole load and no lock it takes is held while the
    import waits.
    Then the snapshot is staged (``stage_snapshot``) and one transaction
    (``engine.begin()``) runs ``load_isolated`` (records, which first checks
    that every record table is empty), ``recount_relation_arrays`` and
    ``import_auth`` (admins to ``_superusers``, token secrets, settings: they
    re-check every precondition under their locks). A failure of any of them,
    ``fail_after`` (records) and ``auth_fail_after`` (``import_auth`` stage)
    included, rolls everything back: no record, account or setting remains,
    and the fresh staging directory created by this call is removed.

    Returns the record count. ``report`` (optional dict) receives the load
    evidence of ``load_isolated``, ``relation_arrays`` (per-field counts),
    ``auth`` (``import_auth``'s value-free report), ``single_transaction`` and
    timings: aggregates only, no secret, hash or token value.

    On success the staged copy is kept for serving (it holds a private copy of
    data.db: destroy it after a rehearsal). A process ended without Python
    unwinding (SIGKILL, the OOM killer, and by default SIGTERM and SIGHUP: a
    timeout, ``docker stop``, a closed terminal) leaves nothing in PostgreSQL
    (the server rolls the open transaction back) but cannot remove
    ``destination``: delete it or use a new path, a retry at the same path
    fails with FileExistsError. Run the import inside
    ``cancelled_by_termination()`` to turn SIGTERM and SIGHUP into a
    cancellation that does run this cleanup.
    """
    from scripts.c11_auth_import import FAIL_STAGES, check_ready, hides_parameters, import_auth

    if auth_fail_after is not None and auth_fail_after not in FAIL_STAGES:
        raise ValueError("unknown failure stage")
    if not hides_parameters(engine):
        # Without it SQLAlchemy repeats bound values (password hashes,
        # tokenKeys, secrets) in its error messages.
        raise ImportBlocked("engine_hide_parameters_required")
    if not diagnostic["ready_for_isolated_rehearsal"]:
        raise ImportBlocked("diagnostic_blockers")  # before any database access
    evidence = report if report is not None else {}
    started = time.monotonic()
    async with engine.connect() as probe:
        await check_ready(probe, data, definitions)
    checked = time.monotonic()
    stage_snapshot(data, destination, diagnostic, manifest=manifest)
    staged = time.monotonic()
    try:
        async with engine.begin() as connection:
            count = await load_isolated(
                connection,
                destination,
                definitions,
                fail_after=fail_after,
                report=evidence,
                batch_size=batch_size,
            )
            loaded = time.monotonic()
            arrays = await recount_relation_arrays(connection, destination, definitions)
            recounted = time.monotonic()
            auth = await import_auth(
                connection, destination, definitions, fail_after=auth_fail_after
            )
            imported = time.monotonic()
    except BaseException:
        shutil.rmtree(destination)
        raise
    evidence["relation_arrays"] = arrays
    evidence["auth"] = auth
    evidence["single_transaction"] = {
        "records": count,
        "relation_arrays_recounted": len(arrays),
        "accounts_and_settings": auth["imported"],
        "committed": True,
    }
    timings = evidence.setdefault("timings_seconds", {})
    timings["readiness_check"] = round(checked - started, 3)
    timings["staging"] = round(staged - checked, 3)
    timings["relation_arrays"] = round(recounted - loaded, 3)
    timings["auth_import"] = round(imported - recounted, 3)
    timings["commit"] = round(time.monotonic() - imported, 3)
    return count


@asynccontextmanager
async def cancelled_by_termination(signals=(signal.SIGTERM, signal.SIGHUP)):
    """Turn SIGTERM and SIGHUP into the cancellation of the current task.

    By default these signals end the Python process without unwinding: no
    rollback request and no removal of the private staging copy. Inside this
    context they cancel the running task instead, so that ``import_snapshot``
    rolls back and removes its staging directory, then the CancelledError
    propagates. SIGKILL and the OOM killer cannot be intercepted.
    """
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    for number in signals:
        loop.add_signal_handler(number, task.cancel)
    try:
        yield
    finally:
        for number in signals:
            loop.remove_signal_handler(number)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--data", type=Path, help="Closed backup directory")
    inputs.add_argument(
        "--archive",
        type=Path,
        help="ZIP inventory only; never extracted (verified extraction: scripts.c11_extract)",
    )
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--source-version", help="Version supplied with this backup; not guessed")
    parser.add_argument(
        "--manifest",
        type=Path,
        help="Private extraction manifest used instead of a storage/ directory",
    )
    parser.add_argument(
        "--target-columns",
        type=Path,
        help="Schema-only target column description (target_spec_json format)",
    )
    parser.add_argument("--report", type=Path, required=True, help="New private JSON report")
    parser.add_argument(
        "--private-report",
        type=Path,
        help="New 0600 file with record-level anomaly details (IDs, references, keys)",
    )
    args = parser.parse_args()
    if args.data and not args.schema:
        parser.error("--schema is required with --data")
    if not args.data and (args.manifest or args.private_report or args.target_columns):
        parser.error("--manifest, --target-columns and --private-report require --data")
    for path in (args.report, args.private_report):
        if path is not None and path_present(path):
            parser.error(f"{path} must be a new path")
    if args.private_report is not None and args.private_report == args.report:
        parser.error("--private-report must differ from --report")
    private = {} if args.private_report else None
    try:
        if args.archive:
            report = inspect_archive(args.archive)
        else:
            spec = None
            if args.target_columns:
                spec = target_spec_from_json(json.loads(args.target_columns.read_text()))
            report = diagnose(
                args.data,
                args.schema,
                source_version=args.source_version,
                manifest=args.manifest,
                target_columns=spec,
                private=private,
            )
    except (ImportBlocked, sqlite3.Error, OSError, zipfile.BadZipFile, ValueError) as exc:
        report = {
            "ready_for_isolated_rehearsal": False,
            "production_authorized": False,
            "error": str(exc) if isinstance(exc, ImportBlocked) else type(exc).__name__,
        }
        private = None
    # Both documents are complete before any file exists: a value that cannot
    # be encoded never leaves one report written and the other one partial.
    public = json_bytes(report)
    details = json_bytes(private, ascii=True) if private is not None else None
    write_bytes_new(args.report, public)
    if details is not None:
        write_bytes_new(args.private_report, details)
    print(
        json.dumps(
            {
                "report": str(args.report),
                "private_report": str(args.private_report) if private is not None else None,
                "ready_for_isolated_rehearsal": report["ready_for_isolated_rehearsal"],
            }
        )
    )
    ok = report["ready_for_isolated_rehearsal"] or report.get("ready_for_offline_extraction_review")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
