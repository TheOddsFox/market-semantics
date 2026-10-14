from dataclasses import replace

import pyarrow.parquet as pq
import pytest

from market_semantics.contracts import (
    SCHEMAS,
    Limits,
    SemanticsError,
    identity,
    json_bytes,
    sha256,
    strict_json,
)
from market_semantics.release import prepare_release, verify_release

from .release_helpers import release_case


def prepared(stage, empty=False):
    tables, bundles, curation, selection = release_case(stage, empty)
    return prepare_release(tables, bundles, curation, selection, stage, Limits())


def test_round_trip_is_offline_and_does_not_compile_or_write(tmp_path, monkeypatch):
    result = prepared(tmp_path)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    monkeypatch.setattr(
        "market_semantics.election.compile_tables", lambda *args: pytest.fail("compiled")
    )
    verified = verify_release(tmp_path, result.manifest_sha256)
    assert verified == result
    assert before == {
        p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
    }
    selection = strict_json((tmp_path / "selection.json").read_bytes())
    assert selection["curation"]["path"] == "curation.json"
    assert b"/operator" not in (tmp_path / "manifest.json").read_bytes()
    assert (tmp_path / "curation.json").read_bytes().startswith(b"  ")


def test_same_inputs_produce_identical_release_bytes(tmp_path):
    left = prepared(tmp_path / "left")
    right = prepared(tmp_path / "right")
    assert left.manifest_sha256 == right.manifest_sha256
    assert left.release_id == right.release_id
    for path in left.path.rglob("*"):
        if path.is_file():
            assert path.read_bytes() == (right.path / path.relative_to(left.path)).read_bytes()


def test_empty_relations_retain_exact_typed_schemas(tmp_path):
    result = prepared(tmp_path, empty=True)
    for name, schema in SCHEMAS.items():
        table = pq.read_table(tmp_path / f"{name}.parquet")
        assert table.schema.equals(schema, check_metadata=True)
        assert table.num_rows == (2 if name == "coverage" else 0)
    assert verify_release(tmp_path, result.manifest_sha256) == result


def test_manifest_pin_is_checked_before_parsing(tmp_path):
    (tmp_path / "manifest.json").write_bytes(b"not JSON")
    with pytest.raises(SemanticsError, match="pin mismatch"):
        verify_release(tmp_path, "0" * 64)


@pytest.mark.parametrize(
    "member", ["entities.parquet", "inputs/kalshi/markets.json", "validation_report.json"]
)
def test_all_retained_bytes_are_in_inventory(tmp_path, member):
    result = prepared(tmp_path)
    with (tmp_path / member).open("ab") as stream:
        stream.write(b"corruption")
    with pytest.raises(SemanticsError):
        verify_release(tmp_path, result.manifest_sha256)


def repin(stage, changed):
    manifest = strict_json((stage / "manifest.json").read_bytes())
    for name in changed:
        data = (stage / name).read_bytes()
        manifest["inventory"][name].update(size_bytes=len(data), sha256=sha256(data))
    body = {key: value for key, value in manifest.items() if key != "release_id"}
    manifest["release_id"] = identity("release", body)
    data = json_bytes(manifest)
    (stage / "manifest.json").write_bytes(data)
    return sha256(data)


def test_rehashed_semantic_corruption_with_trusted_new_pin_still_fails(tmp_path):
    prepared(tmp_path)
    path = tmp_path / "coverage.parquet"
    table = pq.read_table(path)
    rows = table.to_pylist()
    rows[0]["mapped_count"] += 1
    import pyarrow as pa

    pq.write_table(
        pa.Table.from_pylist(rows, schema=table.schema),
        path,
        compression="NONE",
        use_dictionary=False,
        version="2.6",
        row_group_size=1024,
    )
    pin = repin(tmp_path, ["coverage.parquet"])
    with pytest.raises(SemanticsError, match="counts differ"):
        verify_release(tmp_path, pin)


def test_report_is_recomputed_after_repin(tmp_path):
    prepared(tmp_path)
    (tmp_path / "validation_report.json").write_bytes(json_bytes({"valid": True}))
    pin = repin(tmp_path, ["validation_report.json"])
    with pytest.raises(SemanticsError, match="report differs"):
        verify_release(tmp_path, pin)


def test_verifier_enforces_total_bytes_and_does_not_follow_links(tmp_path):
    result = prepared(tmp_path)
    with pytest.raises(SemanticsError):
        verify_release(tmp_path, result.manifest_sha256, replace(Limits(), max_release_bytes=32))
    target = tmp_path / "entities.parquet"
    copy = tmp_path.parent / (tmp_path.name + "-outside")
    target.rename(copy)
    target.symlink_to(copy)
    with pytest.raises(SemanticsError):
        verify_release(tmp_path, result.manifest_sha256)


def test_release_identity_encoding_fixture():
    from market_semantics.release import _manifest

    manifest = _manifest({}, {}, {}, {"source_revision": "synthetic"})
    assert manifest["release_id"] == (
        "release:f3c5448e969d6c5906c0f66cf23ed80a0ee19f874166aaf91486f7e57c6689ca"
    )
    assert manifest["curation_id"] == identity("curation", {})


def test_inventory_file_count_boundary_is_checked_before_members(tmp_path, monkeypatch):
    import market_semantics.release as release

    result = prepared(tmp_path)
    manifest = strict_json((tmp_path / "manifest.json").read_bytes())
    monkeypatch.setattr(release, "MAX_INVENTORY_FILES", len(manifest["inventory"]))
    assert verify_release(tmp_path, result.manifest_sha256) == result
    monkeypatch.setattr(release, "MAX_INVENTORY_FILES", len(manifest["inventory"]) - 1)
    with pytest.raises(SemanticsError, match="inventory size"):
        verify_release(tmp_path, result.manifest_sha256)


def test_manifest_byte_boundary(tmp_path, monkeypatch):
    import market_semantics.release as release

    result = prepared(tmp_path)
    size = (tmp_path / "manifest.json").stat().st_size
    monkeypatch.setattr(release, "MAX_MANIFEST_BYTES", size)
    assert verify_release(tmp_path, result.manifest_sha256) == result
    monkeypatch.setattr(release, "MAX_MANIFEST_BYTES", size - 1)
    with pytest.raises(SemanticsError) as error:
        verify_release(tmp_path, result.manifest_sha256)
    assert error.value.code == "LIMIT_EXCEEDED"


@pytest.mark.parametrize("depth,code", [(64, "INVALID_INPUT"), (65, "LIMIT_EXCEEDED")])
def test_manifest_json_depth_boundary(tmp_path, depth, code):
    body = b"[" * depth + b"null" + b"]" * depth
    (tmp_path / "manifest.json").write_bytes(body)
    with pytest.raises(SemanticsError) as error:
        verify_release(tmp_path, sha256(body))
    assert error.value.code == code


def test_aggregate_inventory_bytes_preflight_happens_before_member_reads(tmp_path, monkeypatch):
    import market_semantics.release as release

    result = prepared(tmp_path)
    manifest = strict_json((tmp_path / "manifest.json").read_bytes())
    manifest["inventory"]["entities.parquet"]["size_bytes"] = Limits().max_release_bytes
    data = json_bytes(manifest)
    (tmp_path / "manifest.json").write_bytes(data)
    original = release._read

    def read_manifest_only(root, name, maximum):
        assert name == "manifest.json", "read an inventory member before preflighting total bytes"
        return original(root, name, maximum)

    monkeypatch.setattr(release, "_read", read_manifest_only)
    with pytest.raises(SemanticsError, match="total byte limit"):
        verify_release(tmp_path, sha256(data))
    assert result.path == tmp_path


def test_staging_byte_budget_is_enforced_before_each_artifact(tmp_path):
    from market_semantics.release import _write_artifact

    limits = replace(Limits(), max_release_bytes=4, max_temp_bytes=4)
    assert _write_artifact(tmp_path, "fits", b"1234", 0, limits) == 4
    with pytest.raises(SemanticsError, match="total byte limit"):
        _write_artifact(tmp_path, "over", b"5", 4, limits)
    assert not (tmp_path / "over").exists()
    assert sum(path.stat().st_size for path in tmp_path.iterdir()) == 4


def test_preparation_never_writes_past_temp_storage_budget(tmp_path):
    tables, bundles, curation, selection = release_case(tmp_path)
    initial_size = sum(path.stat().st_size for path in tmp_path.rglob("*") if path.is_file())
    limits = replace(Limits(), max_temp_bytes=initial_size + 1)
    selection["limits"]["max_temp_bytes"] = limits.max_temp_bytes
    with pytest.raises(SemanticsError, match="total byte limit"):
        prepare_release(tables, bundles, curation, selection, tmp_path, limits)
    assert (
        sum(path.stat().st_size for path in tmp_path.rglob("*") if path.is_file()) == initial_size
    )
    assert not (tmp_path / "manifest.json").exists()


def test_parquet_record_count_boundary_precedes_metadata_decode(tmp_path, monkeypatch):
    import market_semantics.release as release

    prepared(tmp_path)
    data = (tmp_path / "coverage.parquet").read_bytes()
    assert (
        len(
            release._read_parquet(
                data, "coverage", {"row_count": 2}, replace(Limits(), max_records_per_bundle=2)
            )
        )
        == 2
    )
    monkeypatch.setattr(pq, "ParquetFile", lambda *args, **kwargs: pytest.fail("decoded metadata"))
    with pytest.raises(SemanticsError, match="row count"):
        release._read_parquet(
            data, "coverage", {"row_count": 2}, replace(Limits(), max_records_per_bundle=1)
        )


def test_decoded_row_group_boundary(tmp_path, monkeypatch):
    import market_semantics.release as release

    prepared(tmp_path)
    data = (tmp_path / "coverage.parquet").read_bytes()
    group_size = pq.ParquetFile(tmp_path / "coverage.parquet").metadata.row_group(0).total_byte_size
    monkeypatch.setattr(release, "MAX_DECODED_BATCH_BYTES", group_size)
    assert len(release._read_parquet(data, "coverage", {"row_count": 2}, Limits())) == 2
    monkeypatch.setattr(release, "MAX_DECODED_BATCH_BYTES", group_size - 1)
    with pytest.raises(SemanticsError, match="decoded row group"):
        release._read_parquet(data, "coverage", {"row_count": 2}, Limits())


@pytest.mark.parametrize("relation", ["native_outcomes", "outcome_mappings"])
def test_trusted_repin_cannot_accept_unusable_identity_or_broken_claim_reference(
    tmp_path, relation
):
    import pyarrow as pa

    from market_semantics.contracts import assessment_id

    prepared(tmp_path)
    path = tmp_path / f"{relation}.parquet"
    table = pq.read_table(path)
    rows = table.to_pylist()
    if relation == "native_outcomes":
        rows[0]["usable"] = False
    else:
        row = next(row for row in rows if row["status"] == "mapped")
        row["claim_id"] = "claim:" + "0" * 64
        row["assessment_id"] = assessment_id("mapping", row)
    pq.write_table(
        pa.Table.from_pylist(rows, schema=table.schema),
        path,
        compression="NONE",
        use_dictionary=False,
        version="2.6",
        row_group_size=1024,
    )
    pin = repin(tmp_path, [f"{relation}.parquet"])
    with pytest.raises(SemanticsError, match="native projection|accepted mapping"):
        verify_release(tmp_path, pin)


def test_parquet_footer_string_budget_is_enforced_before_rows(tmp_path):
    import pyarrow as pa

    from market_semantics.release import MAX_MANIFEST_BYTES, _read_parquet

    schema = SCHEMAS["coverage"].with_metadata({b"oversized": b"x" * (MAX_MANIFEST_BYTES + 1)})
    sink = pa.BufferOutputStream()
    pq.write_table(pa.Table.from_pylist([], schema=schema), sink, compression="NONE")
    with pytest.raises(SemanticsError, match="invalid Parquet"):
        _read_parquet(sink.getvalue().to_pybytes(), "coverage", {"row_count": 0}, Limits())


def test_rehashed_release_must_obey_its_recorded_storage_limit(tmp_path):
    prepared(tmp_path)
    selection = strict_json((tmp_path / "selection.json").read_bytes())
    selection["limits"]["max_temp_bytes"] = 1
    (tmp_path / "selection.json").write_bytes(json_bytes(selection))
    pin = repin(tmp_path, ["selection.json"])
    with pytest.raises(SemanticsError, match="recorded total byte limit"):
        verify_release(tmp_path, pin)
