"""Bounded child processing and parent-only immutable publication."""

from __future__ import annotations

import fcntl
import multiprocessing
import os
import shutil
import signal
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path

from .contracts import (
    ELECTION_ID,
    MAX_MANIFEST_BYTES,
    VENUE_CONTRACTS,
    Limits,
    SemanticsError,
    require,
    sha256,
    strict_json,
)


def checked_path(value: str | Path, *, exists: bool = True) -> Path:
    require(isinstance(value, (str, Path)), "operator path must be a string", "INVALID_SELECTION")
    path = Path(value)
    require(path.is_absolute(), "operator paths must be absolute", "INVALID_SELECTION")
    require(".." not in path.parts, "parent traversal is not supported")
    for component in reversed((path, *path.parents)):
        try:
            require(not stat.S_ISLNK(component.lstat().st_mode), "symlink paths are not supported")
        except FileNotFoundError:
            require(not exists or component != path, "operator path does not exist")
    return path


def load_selection(path: Path) -> tuple[dict, Limits]:
    from .inputs import read_regular

    selection = strict_json(read_regular(checked_path(path), MAX_MANIFEST_BYTES))
    require(
        isinstance(selection, dict)
        and set(selection) == {"version", "election_id", "inputs", "curation", "limits"},
        "invalid selection fields",
        "INVALID_SELECTION",
    )
    require(
        type(selection["version"]) is int and selection["version"] == 1,
        "unsupported selection version",
        "INVALID_SELECTION",
    )
    require(selection["election_id"] == ELECTION_ID, "unsupported election", "INVALID_SELECTION")
    limits = Limits.from_dict(selection["limits"])
    require(
        isinstance(selection["inputs"], dict) and set(selection["inputs"]) == set(VENUE_CONTRACTS),
        "exactly one input per venue is required",
        "INVALID_SELECTION",
    )
    for venue, source in selection["inputs"].items():
        require(
            isinstance(source, dict)
            and set(source)
            == {"path", "contract", "source_release_id", "manifest_sha256", "selected_market_ids"},
            "invalid selected source fields",
            "INVALID_SELECTION",
        )
        require(source["contract"] == VENUE_CONTRACTS[venue], "unsupported input contract")
        checked_path(source["path"])
        for key in ("source_release_id", "manifest_sha256"):
            require(
                isinstance(source[key], str)
                and len(source[key]) == 64
                and all(x in "0123456789abcdef" for x in source[key]),
                "invalid source digest",
            )
        ids = source["selected_market_ids"]
        require(
            isinstance(ids, list)
            and 0 < len(ids) <= limits.max_markets_per_venue
            and all(isinstance(x, str) and bool(x) for x in ids)
            and len(set(ids)) == len(ids),
            "invalid selected market identities",
            "INVALID_SELECTION",
        )
        source["selected_market_ids"] = sorted(ids)
    curation = selection["curation"]
    require(
        isinstance(curation, dict) and set(curation) == {"path", "sha256"},
        "invalid selected curation",
        "INVALID_SELECTION",
    )
    checked_path(curation["path"])
    require(
        isinstance(curation["sha256"], str)
        and len(curation["sha256"]) == 64
        and all(x in "0123456789abcdef" for x in curation["sha256"]),
        "invalid curation pin",
    )
    return selection, limits


def _worker_entry(connection, operation: str, arguments: tuple) -> None:
    os.setsid()
    try:
        from .release import prepare_release, verify_release

        if operation == "verify":
            root, pin, limit_values = arguments
            result = verify_release(Path(root), pin, Limits.from_dict(limit_values))
        else:
            from .adapters.kalshi import adapt as kalshi_adapt
            from .adapters.polymarket import adapt as polymarket_adapt
            from .election import compile_tables
            from .inputs import load_bundle, read_regular

            selection, stage_name, limit_values = arguments
            stage, limits = Path(stage_name), Limits.from_dict(limit_values)
            budget = min(limits.max_temp_bytes, limits.max_release_bytes)
            curation_bytes = read_regular(
                Path(selection["curation"]["path"]), min(MAX_MANIFEST_BYTES, budget)
            )
            require(
                sha256(curation_bytes) == selection["curation"]["sha256"], "curation pin mismatch"
            )
            curation = strict_json(curation_bytes)
            (stage / "curation.json").write_bytes(curation_bytes)
            (stage / "inputs").mkdir()
            used = len(curation_bytes)
            bundles, projections = {}, []
            for venue, adapter in (("polymarket", polymarket_adapt), ("kalshi", kalshi_adapt)):
                source = selection["inputs"][venue]
                remaining = budget - used
                require(remaining > 0, "staging storage limit exceeded", "LIMIT_EXCEEDED")
                bundles[venue] = load_bundle(
                    venue,
                    Path(source["path"]),
                    stage / "inputs" / venue,
                    source["manifest_sha256"],
                    source["source_release_id"],
                    source["selected_market_ids"],
                    replace(
                        limits,
                        max_bundle_bytes=min(limits.max_bundle_bytes, remaining),
                        max_temp_bytes=remaining,
                    ),
                )
                used += sum(path.stat().st_size for path in bundles[venue].root.iterdir())
                projections.append(adapter(bundles[venue]))
            tables = compile_tables(projections, curation, bundles, limits)
            result = prepare_release(tables, bundles, curation, selection, stage, limits)
        connection.send(
            (
                True,
                {
                    "release_id": result.release_id,
                    "manifest_sha256": result.manifest_sha256,
                    "coverage_summary": result.coverage_summary,
                },
            )
        )
    except SemanticsError as error:
        connection.send((False, {"code": error.code, "message": str(error)}))
    except Exception as error:
        connection.send(
            (False, {"code": "PROCESSING_FAILED", "message": f"{type(error).__name__}: {error}"})
        )
    finally:
        connection.close()


def _stop(process) -> None:
    if process.is_alive():
        try:
            if os.getpgid(process.pid) == process.pid:
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            pass
        process.join(1)
    if process.is_alive():
        try:
            if os.getpgid(process.pid) == process.pid:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
    process.join()


def supervise(operation: str, arguments: tuple, deadline: float, *, target=_worker_entry) -> dict:
    context = multiprocessing.get_context("spawn")
    receiving, sending = context.Pipe(duplex=False)
    process = context.Process(target=target, args=(sending, operation, arguments))
    process.start()
    sending.close()
    try:
        if not receiving.poll(max(0, deadline - time.monotonic())):
            raise SemanticsError("TIMEOUT", "processing deadline exceeded")
        try:
            success, result = receiving.recv()
        except EOFError as error:
            raise SemanticsError("PROCESSING_FAILED", "worker exited without a result") from error
        process.join(max(0, deadline - time.monotonic()))
        require(
            not process.is_alive() and time.monotonic() < deadline,
            "processing deadline exceeded",
            "TIMEOUT",
        )
        require(process.exitcode == 0, "worker did not exit successfully", "PROCESSING_FAILED")
        if not success:
            raise SemanticsError(result["code"], result["message"])
        return result
    finally:
        _stop(process)
        receiving.close()


@contextmanager
def destination_lock(destination: Path):
    filename = f".market-semantics-{sha256(str(destination).encode())}.lock"
    fd = os.open(destination.parent / filename, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        require(stat.S_ISREG(os.fstat(fd).st_mode), "destination lock must be regular")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SemanticsError(
                "LOCK_BUSY", "another publisher holds the destination lock"
            ) from error
        yield
    finally:
        os.close(fd)


def sync_tree(root: Path, deadline: float) -> None:
    for path in sorted(root.rglob("*")):
        require(time.monotonic() < deadline, "processing deadline exceeded", "TIMEOUT")
        require(not path.is_symlink(), "staging contains a symlink")
        if path.is_file():
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    for directory in [*sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True), root]:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def compile_release(selection_path: Path, destination: Path) -> dict:
    started = time.monotonic()
    selection, limits = load_selection(selection_path)
    deadline = started + limits.max_duration_seconds
    destination = checked_path(destination, exists=False)
    for source in selection["inputs"].values():
        root = Path(source["path"])
        require(
            not destination.is_relative_to(root) and not root.is_relative_to(destination),
            "input and output overlap",
        )
    require(not destination.exists(), "output already exists", "OUTPUT_EXISTS")
    destination.parent.mkdir(parents=True, exist_ok=True)
    checked_path(destination.parent)
    with destination_lock(destination):
        require(not destination.exists(), "output already exists", "OUTPUT_EXISTS")
        stage = Path(tempfile.mkdtemp(prefix=".market-semantics-stage-", dir=destination.parent))
        try:
            result = supervise("compile", (selection, str(stage), asdict(limits)), deadline)
            total = sum(p.stat().st_size for p in stage.rglob("*") if p.is_file())
            require(
                total <= min(limits.max_temp_bytes, limits.max_release_bytes),
                "staging storage limit exceeded",
                "LIMIT_EXCEEDED",
            )
            sync_tree(stage, deadline)
            require(time.monotonic() < deadline, "processing deadline exceeded", "TIMEOUT")
            require(not destination.exists(), "output already exists", "OUTPUT_EXISTS")
            os.rename(stage, destination)
            parent = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
            return {
                "status": "compiled",
                "output": str(destination),
                "validation_status": "passed",
                **result,
            }
        finally:
            if stage.exists():
                shutil.rmtree(stage)


def verify_copied_release(root: Path, pin: str) -> dict:
    root = checked_path(root)
    require(
        isinstance(pin, str) and len(pin) == 64 and all(x in "0123456789abcdef" for x in pin),
        "invalid expected manifest digest",
    )
    limits = Limits()
    result = supervise(
        "verify", (str(root), pin, asdict(limits)), time.monotonic() + limits.max_duration_seconds
    )
    return {"status": "verified", "output": str(root), "validation_status": "passed", **result}
