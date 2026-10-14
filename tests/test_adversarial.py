"""Adversarial acceptance checks at copy, publication and verification boundaries."""

import multiprocessing
import multiprocessing.connection
import os
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from market_semantics.adapters.polymarket import adapt
from market_semantics.contracts import (
    SemanticsError,
    canonical_json,
    identity,
    json_bytes,
    sha256,
    strict_json,
)
from market_semantics.inputs import load_bundle, verify_bundle
from market_semantics.release import verify_release
from market_semantics.runtime import compile_release, load_selection

from .adapter_helpers import relations, write_bundle
from .fixtures import make_case
from .test_release import prepared, repin


def _partial_write_child(connection, operation, arguments):
    from market_semantics import runtime

    selection, stage, limits, ready, phase = arguments

    class BlockedWriter:
        def __init__(self, stream):
            self.stream = stream

        def write(self, data):
            self.stream.write(data[: max(1, len(data) // 2)])
            self.stream.flush()
            ready.set()
            multiprocessing.Event().wait()

    @contextmanager
    def blocked_stream(stream):
        with stream:
            yield BlockedWriter(stream)

    if phase == "copy":
        original = os.fdopen

        def fdopen(fd, mode="r", *args, **kwargs):
            stream = original(fd, mode, *args, **kwargs)
            return blocked_stream(stream) if mode == "wb" else stream

        os.fdopen = fdopen
    else:
        original = Path.open

        def path_open(path, mode="r", *args, **kwargs):
            stream = original(path, mode, *args, **kwargs)
            return blocked_stream(stream) if mode == "xb" and path.suffix == ".parquet" else stream

        Path.open = path_open
    runtime._worker_entry(connection, operation, (selection, stage, limits))


@pytest.mark.parametrize("phase", ["copy", "parquet"])
def test_cancellation_during_partial_writes_reaps_worker_and_removes_stage(
    tmp_path, monkeypatch, phase
):
    from market_semantics import runtime

    monkeypatch.syspath_prepend(str(Path(__file__).parents[1]))
    selection_path, _, _ = make_case(tmp_path / "operator")
    previous = tmp_path / "previous"
    previous.mkdir()
    (previous / "retained").write_bytes(b"previous immutable release")
    output = tmp_path / "next"
    ready = multiprocessing.get_context("spawn").Event()
    before = {child.pid for child in multiprocessing.active_children()}
    original = runtime.supervise

    def supervise(operation, arguments, deadline):
        return original(
            operation, (*arguments, ready, phase), deadline, target=_partial_write_child
        )

    def interrupt_at_write_barrier(connection, timeout=0):
        assert ready.wait(15), "worker never reached the partial-write barrier"
        (stage,) = tmp_path.glob(".market-semantics-stage-*")
        partials = list(
            stage.glob("*.parquet" if phase == "parquet" else "inputs/polymarket/*.json")
        )
        assert partials and any(path.stat().st_size > 0 for path in partials)
        assert not output.exists()
        raise KeyboardInterrupt

    monkeypatch.setattr(runtime, "supervise", supervise)
    monkeypatch.setattr(multiprocessing.connection.Connection, "poll", interrupt_at_write_barrier)
    with pytest.raises(KeyboardInterrupt):
        compile_release(selection_path, output)
    assert not output.exists()
    assert not list(tmp_path.glob(".market-semantics-stage-*"))
    assert {child.pid for child in multiprocessing.active_children()} == before
    assert (previous / "retained").read_bytes() == b"previous immutable release"


@pytest.mark.parametrize("timing", ["before", "after"])
def test_source_mutation_at_read_barrier_never_copies_unverified_bytes(
    tmp_path, monkeypatch, timing
):
    from market_semantics import inputs

    source, destination = tmp_path / "source", tmp_path / "copied"
    pin, release = write_bundle(source, "polymarket")
    original_bytes = (source / "markets.json").read_bytes()
    original = inputs._read

    def mutate_at_read_barrier(fd, name, maximum):
        if name == "markets.json" and timing == "before":
            (source / name).write_bytes(b"[]")
        body = original(fd, name, maximum)
        if name == "markets.json" and timing == "after":
            (source / name).write_bytes(b"[]")
        return body

    monkeypatch.setattr(inputs, "_read", mutate_at_read_barrier)
    if timing == "before":
        with pytest.raises(SemanticsError, match="integrity"):
            load_bundle("polymarket", source, destination, pin, release, ["1"])
        assert not destination.exists()
    else:
        load_bundle("polymarket", source, destination, pin, release, ["1"])
        monkeypatch.setattr(inputs, "_read", original)
        assert (source / "markets.json").read_bytes() != original_bytes
        assert (destination / "markets.json").read_bytes() == original_bytes
        verify_bundle("polymarket", destination, pin, release, ["1"])


def test_shuffled_operator_selection_has_identical_release_bytes(tmp_path):
    selection_path, selection, _ = make_case(tmp_path / "operator")
    for venue, extra in (("polymarket", "2"), ("kalshi", "N")):
        source = selection["inputs"][venue]
        rows = {
            path.stem: strict_json(path.read_bytes())
            for path in Path(source["path"]).glob("*.json")
            if path.name != "manifest.json"
        }
        key = "market_id" if venue == "polymarket" else "market_ticker"
        rows["coverage"].append({key: extra, "status": "absent", "error": None})
        expanded = tmp_path / ("expanded-" + venue)
        pin, release = write_bundle(expanded, venue, rows)
        source.update(path=str(expanded), manifest_sha256=pin, source_release_id=release)
        source["selected_market_ids"].append(extra)
    selection_path.write_bytes(json_bytes(selection))
    first, second = tmp_path / "first", tmp_path / "second"
    first_result = compile_release(selection_path, first)
    selection["inputs"] = dict(reversed(list(selection["inputs"].items())))
    for source in selection["inputs"].values():
        source["selected_market_ids"].reverse()
    selection_path.write_bytes(json_bytes(selection))
    second_result = compile_release(selection_path, second)
    assert first_result["release_id"] == second_result["release_id"]
    assert {
        path.relative_to(first): path.read_bytes() for path in first.rglob("*") if path.is_file()
    } == {
        path.relative_to(second): path.read_bytes() for path in second.rglob("*") if path.is_file()
    }


def test_selection_accepts_100_markets_but_rejects_101_even_with_raised_limit(tmp_path):
    selection_path, selection, _ = make_case(tmp_path / "operator")
    selection["inputs"]["polymarket"]["selected_market_ids"] = [str(i) for i in range(1, 101)]
    selection_path.write_bytes(json_bytes(selection))
    assert (
        len(load_selection(selection_path)[0]["inputs"]["polymarket"]["selected_market_ids"]) == 100
    )
    selection["inputs"]["polymarket"]["selected_market_ids"].append("101")
    selection["limits"]["max_markets_per_venue"] = 101
    selection_path.write_bytes(json_bytes(selection))
    with pytest.raises(SemanticsError):
        load_selection(selection_path)


@pytest.mark.parametrize(
    "relation,field,value",
    [
        ("entities", "identity_json", canonical_json({"round": "primary"})),
        ("claims", "scope_json", canonical_json({"round": "primary"})),
        ("native_outcomes", "source_updated_at_raw_json", '"2020-01-01T00:00:00Z"'),
    ],
)
def test_trusted_repin_cannot_rewrite_identity_scope_or_source_timing(
    tmp_path, relation, field, value
):
    prepared(tmp_path)
    path = tmp_path / f"{relation}.parquet"
    table = pq.read_table(path)
    rows = table.to_pylist()
    assert field in table.schema.names
    rows[0][field] = value
    pq.write_table(
        pa.Table.from_pylist(rows, schema=table.schema),
        path,
        compression="NONE",
        use_dictionary=False,
        version="2.6",
        row_group_size=1024,
    )
    pin = repin(tmp_path, [path.name])
    with pytest.raises(SemanticsError):
        verify_release(tmp_path, pin)


@pytest.mark.parametrize("corruption", ["schema", "row_count", "missing_relation"])
def test_self_consistent_outer_inventory_cannot_change_relation_contract(tmp_path, corruption):
    prepared(tmp_path)
    path = tmp_path / "manifest.json"
    manifest = strict_json(path.read_bytes())
    item = manifest["inventory"]["claims.parquet"]
    if corruption == "schema":
        item["schema"][0]["nullable"] = not item["schema"][0]["nullable"]
    elif corruption == "row_count":
        item["row_count"] += 1
    else:
        del manifest["inventory"]["claims.parquet"]
        (tmp_path / "claims.parquet").unlink()
    manifest["release_id"] = identity(
        "release", {key: value for key, value in manifest.items() if key != "release_id"}
    )
    data = json_bytes(manifest)
    path.write_bytes(data)
    with pytest.raises(SemanticsError):
        verify_release(tmp_path, sha256(data))


def test_equal_asset_numbers_in_different_protocol_kinds_remain_distinct(tmp_path):
    rows = relations("polymarket")
    market = deepcopy(rows["markets"][0])
    market.update(market_id="2", source_market_version="v2", condition_id=None)
    rows["markets"].append(market)
    for original in list(rows["outcomes"]):
        row = deepcopy(original)
        row.update(
            market_id="2",
            source_market_version="v2",
            asset_kind="poly_v2_position",
            position_id=row["asset_id"],
            clob_token_id=None,
            condition_id=None,
        )
        rows["outcomes"].append(row)
    rows["coverage"].append({"market_id": "2", "status": "found", "error": None})
    pin, release = write_bundle(tmp_path / "source", "polymarket", rows)
    projection = adapt(verify_bundle("polymarket", tmp_path / "source", pin, release, ["1", "2"]))
    outcomes = projection.native_outcomes
    assert len({row["native_outcome_revision_id"] for row in outcomes}) == 4
    assert {row["asset_kind"] for row in outcomes} == {"ctf_token", "poly_v2_position"}
    assert all(row["usable"] for row in outcomes)


def test_complete_cross_venue_policy_accepts_only_same_claim_or_explicit_inverse(tmp_path):
    selection_path, _, _ = make_case(tmp_path / "operator", curated=True)
    output = tmp_path / "release"
    result = compile_release(selection_path, output)
    assert verify_release(output, result["manifest_sha256"])
    natives = {
        row["native_outcome_revision_id"]: row
        for row in pq.read_table(output / "native_outcomes.parquet").to_pylist()
    }
    cross_venue = [
        row
        for row in pq.read_table(output / "relationships.parquet").to_pylist()
        if natives[row["left_revision_id"]]["venue"] != natives[row["right_revision_id"]]["venue"]
        and row["relationship_type"] in {"equivalent", "inverse"}
    ]
    assert len(cross_venue) == 4
    for row in cross_venue:
        same_side = (
            natives[row["left_revision_id"]]["outcome_label"].lower()
            == natives[row["right_revision_id"]]["outcome_label"].lower()
        )
        assert row["status"] == "accepted"
        assert row["relationship_type"] == ("equivalent" if same_side else "inverse")
