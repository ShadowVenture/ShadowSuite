"""Status files: JSON documents the runtime writes and the web UI only reads.

The paper host is the ledger's single writer and DuckDB allows no reader next to a writer, so
the UI never opens a store. Each producer writes ONE file, `<status dir>/<producer>.json`,
atomically (temp file, fsync, rename; mode 600). A status file is a current-state report, never
a record: it is replaced on every export, and nothing decides risk or execution from it.
"""

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, get_args

from shadow_trading_suite.core.fs import private_dirs

STATUS_SCHEMA = 1
MAX_STATUS_BYTES = 8 * 1024 * 1024
Producer = Literal["runtime", "plugins"]
PRODUCERS: tuple[Producer, ...] = get_args(Producer)
AnyProducer = Producer
ALL_PRODUCERS: tuple[AnyProducer, ...] = PRODUCERS


class StatusFileError(ValueError):
    pass


@dataclass(frozen=True)
class StatusDoc:
    producer: AnyProducer
    produced_at: datetime
    payload: Mapping[str, Any]


def status_dir(data_root: Path, data_subdir: str) -> Path:
    return data_root / data_subdir / "status"


def write_status(
    directory: Path, producer: AnyProducer, payload: Mapping[str, Any], now: datetime
) -> Path:
    if producer not in ALL_PRODUCERS:
        raise StatusFileError(f"unknown status producer {producer!r}")
    if now.tzinfo is None:
        raise StatusFileError("produced_at must be timezone-aware")
    doc = {
        "schema": STATUS_SCHEMA,
        "producer": producer,
        "produced_at": now.isoformat(),
        "paper_only": True,
        "payload": payload,
    }
    data = json.dumps(doc, default=str, sort_keys=True).encode()
    if len(data) > MAX_STATUS_BYTES:
        raise StatusFileError(f"{producer} status exceeds {MAX_STATUS_BYTES} bytes")
    private_dirs(directory)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{producer}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        target = directory / f"{producer}.json"
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)  # our own temp file only
        raise
    return target


def parse_status(data: bytes, producer: AnyProducer) -> StatusDoc:
    if len(data) > MAX_STATUS_BYTES:
        raise StatusFileError(f"{producer} status exceeds {MAX_STATUS_BYTES} bytes")
    try:
        doc = json.loads(data)
    except ValueError as exc:
        raise StatusFileError(f"{producer} status is not JSON: {exc}") from exc
    if not isinstance(doc, dict) or doc.get("schema") != STATUS_SCHEMA:
        raise StatusFileError(f"{producer} status has an unsupported schema")
    if doc.get("producer") != producer or doc.get("paper_only") is not True:
        raise StatusFileError(f"{producer} status names another producer or is not paper-only")
    try:
        at = datetime.fromisoformat(str(doc["produced_at"]))
    except (KeyError, ValueError) as exc:
        raise StatusFileError(f"{producer} status has no valid produced_at") from exc
    if at.tzinfo is None or not isinstance(doc.get("payload"), dict):
        raise StatusFileError(f"{producer} status is malformed")
    return StatusDoc(producer, at, doc["payload"])


def read_one(directory: Path, producer: AnyProducer) -> StatusDoc | StatusFileError | None:
    """One producer's current status: None when absent, an error when unreadable or bad."""
    path = directory / f"{producer}.json"
    try:
        with path.open("rb") as fh:
            data = fh.read(MAX_STATUS_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return StatusFileError(f"{producer} status unreadable: {exc}")
    try:
        return parse_status(data, producer)
    except StatusFileError as exc:
        return exc


def read_all_status(directory: Path) -> dict[Producer, StatusDoc | StatusFileError]:
    """Every producer's current status; a missing file is absent, a bad one is an error."""
    out: dict[Producer, StatusDoc | StatusFileError] = {}
    for producer in PRODUCERS:
        doc = read_one(directory, producer)
        if doc is not None:
            out[producer] = doc
    return out
