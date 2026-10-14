import pytest

from market_semantics.contracts import Limits, SemanticsError
from market_semantics.inputs import load_bundle, read_regular, verify_bundle

from .adapter_helpers import relations, write_bundle


@pytest.mark.parametrize("venue,selected", [("polymarket", ["1"]), ("kalshi", ["M"])])
def test_copy_and_verify(tmp_path, venue, selected):
    source, retained = tmp_path / "source", tmp_path / "retained"
    pin, release = write_bundle(source, venue)
    copied = load_bundle(venue, source, retained, pin, release, selected)
    verified = verify_bundle(venue, retained, pin, release, selected)
    assert copied.relations == verified.relations
    assert copied.root == retained
    assert all((source / p.name).read_bytes() == p.read_bytes() for p in retained.iterdir())


@pytest.mark.parametrize("venue,selected", [("polymarket", ["1"]), ("kalshi", ["M"])])
def test_inventory_encoding_is_venue_specific(tmp_path, venue, selected):
    pin, release = write_bundle(tmp_path / "source", venue, newline=venue != "polymarket")
    with pytest.raises(SemanticsError, match="release inventory"):
        verify_bundle(venue, tmp_path / "source", pin, release, selected)


def test_pin_and_selection_are_external(tmp_path):
    source = tmp_path / "source"
    pin, release = write_bundle(source, "polymarket")
    with pytest.raises(SemanticsError, match="pin mismatch"):
        verify_bundle("polymarket", source, "0" * 64, release, ["1"])
    with pytest.raises(SemanticsError, match="selection mismatch"):
        verify_bundle("polymarket", source, pin, release, ["2"])


def test_symlink_and_nonregular_members_are_rejected(tmp_path):
    source = tmp_path / "source"
    pin, release = write_bundle(source, "polymarket")
    target = source / "markets.json"
    moved = tmp_path / "market-copy"
    target.rename(moved)
    target.symlink_to(moved)
    with pytest.raises(SemanticsError):
        verify_bundle("polymarket", source, pin, release, ["1"])
    with pytest.raises(SemanticsError):
        read_regular(source, 1024)
    with pytest.raises(SemanticsError, match="byte limit"):
        read_regular(moved, 1)


def test_duplicate_json_keys_rejected_before_projection(tmp_path):
    source = tmp_path / "source"
    write_bundle(source, "polymarket")
    manifest = source / "manifest.json"
    manifest.write_text('{"contract":1,"contract":2}')
    from market_semantics.contracts import sha256

    with pytest.raises(SemanticsError, match="duplicate JSON key"):
        verify_bundle("polymarket", source, sha256(manifest.read_bytes()), "ignored", ["1"])


def test_current_conflicts_fail_but_raw_history_remains_diagnostic(tmp_path):
    rows = relations("polymarket")
    rows["identity_history"] = [
        {
            "market_id": "1",
            "usable": False,
            "identities": [{"asset_kind": "ctf_token", "asset_id": "11"}],
        }
    ]
    source = tmp_path / "history"
    pin, release = write_bundle(source, "polymarket", rows)
    verify_bundle("polymarket", source, pin, release, ["1"])
    rows["outcomes"][1]["asset_id"] = rows["outcomes"][1]["clob_token_id"] = "11"
    source = tmp_path / "conflict"
    pin, release = write_bundle(source, "polymarket", rows)
    with pytest.raises(SemanticsError, match="duplicate usable"):
        verify_bundle("polymarket", source, pin, release, ["1"])


def test_byte_and_record_limits(tmp_path):
    source = tmp_path / "source"
    pin, release = write_bundle(source, "polymarket")
    with pytest.raises(SemanticsError, match="record limit"):
        verify_bundle("polymarket", source, pin, release, ["1"], Limits(max_records_per_bundle=1))
    with pytest.raises(SemanticsError, match="byte limit"):
        verify_bundle("polymarket", source, pin, release, ["1"], Limits(max_bundle_bytes=1))


def test_kalshi_parent_must_match_native_payload(tmp_path):
    rows = relations("kalshi")
    rows["events"][0]["series_ticker"] = "OTHER"
    pin, release = write_bundle(tmp_path / "source", "kalshi", rows)
    with pytest.raises(SemanticsError, match="payload series differs"):
        verify_bundle("kalshi", tmp_path / "source", pin, release, ["M"])


@pytest.mark.parametrize("asset", ["0", "01", str(2**256)])
def test_noncanonical_native_asset_is_rejected(tmp_path, asset):
    rows = relations("polymarket")
    rows["outcomes"][0]["asset_id"] = rows["outcomes"][0]["clob_token_id"] = asset
    pin, release = write_bundle(tmp_path / "source", "polymarket", rows)
    with pytest.raises(SemanticsError, match="invalid asset ID"):
        verify_bundle("polymarket", tmp_path / "source", pin, release, ["1"])


def test_stale_outcome_cannot_inherit_current_market_version(tmp_path):
    rows = relations("polymarket")
    rows["outcomes"][0]["observation_id"] = "older-observation"
    pin, release = write_bundle(tmp_path / "source", "polymarket", rows)
    with pytest.raises(SemanticsError, match="observation differs"):
        verify_bundle("polymarket", tmp_path / "source", pin, release, ["1"])


def test_directory_symlink_is_not_traversed(tmp_path):
    source = tmp_path / "source"
    pin, release = write_bundle(source, "polymarket")
    alias = tmp_path / "alias"
    alias.symlink_to(source, target_is_directory=True)
    with pytest.raises(SemanticsError, match="filesystem"):
        verify_bundle("polymarket", alias, pin, release, ["1"])
