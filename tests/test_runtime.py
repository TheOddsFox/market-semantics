import json
import multiprocessing
import os
import time
from dataclasses import asdict
from pathlib import Path

import pytest

from market_semantics.contracts import Limits, SemanticsError, sha256
from market_semantics.runtime import (
    _worker_entry,
    compile_release,
    destination_lock,
    supervise,
    verify_copied_release,
)

from .fixtures import make_case


@pytest.fixture(autouse=True)
def spawn_test_imports(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]))


def _blocked_child(connection, operation, arguments):
    os.setsid()
    multiprocessing.Event().wait()


def _lock_child(connection, operation, arguments):
    try:
        with destination_lock(Path(arguments[0])):
            connection.send((True, {"lock": "acquired"}))
    except SemanticsError as error:
        connection.send((True, {"lock": error.code}))
    finally:
        connection.close()


def test_roundtrip_from_new_location_and_determinism(tmp_path):
    selection_path, _, _ = make_case(tmp_path / "operator", curated=True)
    first, second = tmp_path / "one", tmp_path / "two"
    result = compile_release(selection_path, first)
    assert result["status"] == "compiled"
    assert verify_copied_release(first, result["manifest_sha256"])["status"] == "verified"
    again = compile_release(selection_path, second)
    assert result["release_id"] == again["release_id"]
    for path in first.rglob("*"):
        if path.is_file():
            assert path.read_bytes() == (second / path.relative_to(first)).read_bytes()
    with pytest.raises(SemanticsError, match="already exists"):
        compile_release(selection_path, first)


def test_supervisor_timeout_terminates_and_reaps():
    before = {p.pid for p in multiprocessing.active_children()}
    started = time.monotonic()
    with pytest.raises(SemanticsError) as captured:
        supervise("blocked", (), time.monotonic() + 1, target=_blocked_child)
    assert captured.value.code == "TIMEOUT"
    assert time.monotonic() - started < 5
    assert {p.pid for p in multiprocessing.active_children()} == before


def test_competing_publisher_uses_persistent_lock(tmp_path):
    destination = tmp_path / "release"
    with destination_lock(destination):
        result = supervise("lock", (str(destination),), time.monotonic() + 10, target=_lock_child)
    assert result == {"lock": "LOCK_BUSY"}
    with destination_lock(destination):
        assert len(list(tmp_path.glob(".market-semantics-*.lock"))) == 1


def test_bad_pin_leaves_no_release_or_staging(tmp_path):
    selection_path, selection, _ = make_case(tmp_path / "operator")
    selection["curation"]["sha256"] = "0" * 64
    selection_path.write_text(json.dumps(selection))
    output = tmp_path / "release"
    with pytest.raises(SemanticsError, match="curation pin"):
        compile_release(selection_path, output)
    assert not output.exists()
    assert not list(tmp_path.glob(".market-semantics-stage-*"))


def test_post_commit_fsync_failure_preserves_verifiable_release(tmp_path, monkeypatch):
    selection_path, _, _ = make_case(tmp_path / "operator")
    output = tmp_path / "release"
    original = os.fsync

    def fsync(fd):
        if output.exists():
            raise OSError("synthetic acknowledgement failure")
        return original(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    with pytest.raises(OSError, match="acknowledgement"):
        compile_release(selection_path, output)
    assert output.is_dir()
    pin = sha256((output / "manifest.json").read_bytes())
    assert verify_copied_release(output, pin)["status"] == "verified"


def test_output_inside_source_refused(tmp_path):
    selection_path, selection, _ = make_case(tmp_path / "operator")
    with pytest.raises(SemanticsError, match="overlap"):
        compile_release(selection_path, Path(selection["inputs"]["polymarket"]["path"]) / "release")


def test_pre_commit_interruption_preserves_prior_release(tmp_path, monkeypatch):
    selection_path, _, _ = make_case(tmp_path / "operator")
    previous = tmp_path / "previous"
    previous.mkdir()
    (previous / "retained").write_bytes(b"previous release")
    output = tmp_path / "next"

    def interrupt_at_barrier(stage, deadline):
        assert (stage / "manifest.json").is_file()
        assert not output.exists()
        raise KeyboardInterrupt

    monkeypatch.setattr("market_semantics.runtime.sync_tree", interrupt_at_barrier)
    with pytest.raises(KeyboardInterrupt):
        compile_release(selection_path, output)
    assert not output.exists()
    assert not list(tmp_path.glob(".market-semantics-stage-*"))
    assert (previous / "retained").read_bytes() == b"previous release"


def test_post_commit_interruption_preserves_completed_release(tmp_path, monkeypatch):
    selection_path, _, _ = make_case(tmp_path / "operator")
    output = tmp_path / "next"
    rename = os.rename

    def interrupt_at_barrier(source, destination):
        rename(source, destination)
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "rename", interrupt_at_barrier)
    with pytest.raises(KeyboardInterrupt):
        compile_release(selection_path, output)
    pin = sha256((output / "manifest.json").read_bytes())
    assert verify_copied_release(output, pin)["status"] == "verified"


def test_invalid_selection_path_type_is_bounded_error(tmp_path):
    selection_path, selection, _ = make_case(tmp_path / "operator")
    selection["curation"]["path"] = None
    selection_path.write_text(json.dumps(selection))
    with pytest.raises(SemanticsError, match="must be a string"):
        compile_release(selection_path, tmp_path / "next")


@pytest.mark.parametrize("limit", ["max_temp_bytes", "max_release_bytes"])
def test_copy_respects_aggregate_storage_before_writing(tmp_path, monkeypatch, limit):
    _, selection, _ = make_case(tmp_path / "operator")
    sizes = [
        sum(path.stat().st_size for path in Path(source["path"]).iterdir())
        for source in selection["inputs"].values()
    ]
    cap = max(sizes) + 1
    stage = tmp_path / "stage"
    stage.mkdir()
    result = []

    class Connection:
        def send(self, message):
            result.append(message)

        def close(self):
            pass

    monkeypatch.setattr(os, "setsid", lambda: None)
    limits = Limits.from_dict({limit: cap})
    _worker_entry(Connection(), "compile", (selection, str(stage), asdict(limits)))
    assert result[0][0] is False
    assert result[0][1]["code"] == "LIMIT_EXCEEDED"
    assert sum(path.stat().st_size for path in stage.rglob("*") if path.is_file()) <= cap
