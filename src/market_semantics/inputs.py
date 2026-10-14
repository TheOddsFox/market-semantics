"""Bounded, offline verification of the two targeted producer contracts."""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from .contracts import (
    MAX_MANIFEST_BYTES,
    VENUE_CONTRACTS,
    VENUE_FILES,
    Limits,
    SemanticsError,
    VerifiedBundle,
    canonical_json,
    require,
    sha256,
    strict_json,
    timestamp,
)


def _directory(path: Path) -> int:
    """Walk from the filesystem root with pinned descriptors, never symlinks."""
    path = Path(os.path.abspath(path))
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read(fd: int, name: str, maximum: int) -> bytes:
    child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        info = os.fstat(child)
        require(stat.S_ISREG(info.st_mode), "bundle member is not a regular file")
        require(info.st_size <= maximum, "bundle exceeds byte limit", "LIMIT_EXCEEDED")
        chunks, remaining = [], maximum + 1
        while remaining:
            chunk = os.read(child, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        body = b"".join(chunks)
        require(len(body) <= maximum, "bundle exceeds byte limit", "LIMIT_EXCEEDED")
        return body
    finally:
        os.close(child)


def _validate_rows(venue: str, relations: dict, selected: tuple[str, ...]) -> None:
    key = "market_id" if venue == "polymarket" else "market_ticker"
    coverage = relations["coverage"]
    require(len(coverage) == len(selected), "coverage does not equal requested selection")
    require(all(isinstance(r.get(key), str) for r in coverage), "invalid coverage identity")
    require({r[key] for r in coverage} == set(selected), "coverage selection mismatch")
    allowed = {"found", "absent", "failed"}
    if venue == "kalshi":
        allowed |= {"unresolved"}
    for row in coverage:
        require(row.get("status") in allowed, "invalid producer coverage status")
        reason = row.get("error")
        require(reason is None or isinstance(reason, str), "invalid producer reason")
    markets = relations["markets"]
    seen = set()
    for row in markets:
        native = row.get(key)
        require(isinstance(native, str) and native in selected, "unselected market")
        require(native not in seen, "duplicate current market identity")
        seen.add(native)
        require(type(row.get("usable")) is bool, "market lacks usability")
        require(
            isinstance(row.get("observation_id"), str) and row["observation_id"],
            "market lacks source observation",
        )
        require(timestamp(row.get("received_at")) is not None, "market lacks receipt timestamp")
    found = {r[key] for r in coverage if r["status"] == "found"}
    require(seen == found, "found coverage and current markets differ")
    if venue == "kalshi":
        for relation, parent_key in (("events", "event_ticker"), ("series", "series_ticker")):
            ids = [r.get(parent_key) for r in relations[relation]]
            require(
                all(isinstance(x, str) and x for x in ids) and len(ids) == len(set(ids)),
                "duplicate or missing authoritative parent identity",
            )
        events = {r["event_ticker"]: r for r in relations["events"]}
        series = {r["series_ticker"] for r in relations["series"]}
        for relation, identity_key in (
            ("markets", "market_ticker"),
            ("events", "event_ticker"),
            ("series", "series_ticker"),
        ):
            for record in relations[relation]:
                require(isinstance(record.get("payload_json"), str), "missing native payload")
                payload = strict_json(record["payload_json"])
                require(isinstance(payload, dict), "native payload is not an object")
                native_key = "event_ticker" if relation == "events" else "ticker"
                require(
                    payload.get(native_key) == record[identity_key],
                    "native payload identity differs from projection",
                )
                require(
                    timestamp(record.get("received_at")) is not None
                    and isinstance(record.get("observation_id"), str)
                    and bool(record["observation_id"]),
                    "missing parent/source provenance",
                )
                if relation == "markets":
                    require(
                        payload.get("event_ticker") == record.get("event_ticker"),
                        "native payload parent differs",
                    )
                if relation == "events":
                    require(
                        payload.get("series_ticker") == record.get("series_ticker"),
                        "native payload series differs",
                    )
        for row in markets:
            if not row["usable"]:
                continue
            parent = events.get(row.get("event_ticker"))
            require(
                parent
                and parent.get("series_ticker") == row.get("series_ticker")
                and row.get("series_ticker") in series,
                "missing authoritative parent",
            )
            require(row.get("source_kind") == "direct", "market is not authoritative")
        return
    by_market = {r[key]: r for r in markets}
    owners, ordinals = set(), set()
    for row in relations["outcomes"]:
        require(
            isinstance(row.get("market_id"), str) and row["market_id"] in by_market,
            "outcome lacks selected parent",
        )
        require(type(row.get("usable")) is bool, "outcome lacks usability")
        market = by_market[row["market_id"]]
        if not row["usable"] or not market["usable"]:
            continue
        require(
            row.get("observation_id") == market["observation_id"],
            "outcome observation differs from current market",
        )
        version = row.get("source_market_version", market.get("source_market_version"))
        require(
            isinstance(version, str) and version.lower() in {"v1", "v2"},
            "unsupported source market version",
        )
        require(
            version == market.get("source_market_version"),
            "outcome source version differs from current market",
        )
        kind = "ctf_token" if version.lower() == "v1" else "poly_v2_position"
        require(row.get("asset_kind") == kind, "asset kind differs from source version")
        asset = row.get("asset_id")
        require(
            isinstance(asset, str)
            and re.fullmatch(r"[1-9][0-9]{0,77}", asset)
            and int(asset) < 2**256,
            "invalid asset ID",
        )
        identity_field = "clob_token_id" if kind == "ctf_token" else "position_id"
        require(row.get(identity_field) == asset, "asset ID differs from typed identity")
        condition = row.get("condition_id")
        require(condition == market.get("condition_id"), "condition identity differs")
        if kind == "ctf_token":
            require(
                isinstance(condition, str) and re.fullmatch(r"0x[0-9a-f]{64}", condition),
                "invalid CTF condition ID",
            )
        require(row.get("chain_index_set") is None, "ordinal is not a chain index set")
        ordinal = row.get("outcome_index")
        require(type(ordinal) is int and 0 < ordinal <= 2**31 - 1, "invalid outcome ordinal")
        require(
            isinstance(row.get("outcome_label"), str) and row["outcome_label"],
            "missing outcome label",
        )
        require((kind, asset) not in owners, "duplicate usable current native identity")
        require((row["market_id"], ordinal) not in ordinals, "duplicate current ordinal")
        owners.add((kind, asset))
        ordinals.add((row["market_id"], ordinal))
        require(timestamp(row.get("received_at")) is not None, "outcome lacks receipt timestamp")
    for market in markets:
        if market["usable"]:
            require(
                any(native == market["market_id"] for native, _ in ordinals),
                "usable market has no usable outcomes",
            )
    for relation in ("memberships", "identity_history"):
        require(
            all(
                isinstance(r.get("market_id"), str) and r["market_id"] in selected
                for r in relations[relation]
            ),
            "unselected diagnostic record",
        )


def _bundle(
    venue,
    root,
    expected_manifest_sha256,
    source_release_id,
    selected_market_ids,
    limits,
    destination=None,
):
    require(venue in VENUE_CONTRACTS, "unsupported venue")
    selected = tuple(selected_market_ids)
    require(0 < len(selected) <= limits.max_markets_per_venue, "invalid selection size")
    require(
        all(isinstance(x, str) and x for x in selected) and len(set(selected)) == len(selected),
        "invalid selected identities",
    )
    native_pattern = (
        r"[1-9][0-9]{0,19}" if venue == "polymarket" else r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}"
    )
    require(
        all(re.fullmatch(native_pattern, value) for value in selected),
        "invalid selected native identity",
    )
    fd = out = None
    try:
        fd = _directory(Path(root))
        names = {f"{x}.json" for x in VENUE_FILES[venue]}
        require(set(os.listdir(fd)) == names | {"manifest.json"}, "unexpected bundle files")
        manifest_body = _read(fd, "manifest.json", min(MAX_MANIFEST_BYTES, limits.max_bundle_bytes))
        require(sha256(manifest_body) == expected_manifest_sha256, "manifest pin mismatch")
        manifest = strict_json(manifest_body)
        require(
            isinstance(manifest, dict)
            and manifest.get("contract") == VENUE_CONTRACTS[venue]
            and manifest.get("coverage_scope") == "targeted",
            "unsupported metadata contract",
        )
        require(manifest.get("source_release_id") == source_release_id, "release pin mismatch")
        require(
            isinstance(manifest.get("files"), dict) and set(manifest["files"]) == names,
            "invalid producer inventory",
        )
        require(timestamp(manifest.get("received_at")) is not None, "missing bundle receipt")
        total, count, inventory, relations, bodies = len(manifest_body), 0, {}, {}, {}
        for name in sorted(names):
            body = _read(fd, name, limits.max_bundle_bytes - total)
            total += len(body)
            inventory[name] = {"size": len(body), "sha256": sha256(body)}
            require(inventory[name] == manifest["files"][name], "producer file integrity mismatch")
            values = strict_json(body)
            require(
                isinstance(values, list) and all(isinstance(x, dict) for x in values),
                "producer relation must be an object array",
            )
            count += len(values)
            require(
                count <= limits.max_records_per_bundle, "record limit exceeded", "LIMIT_EXCEEDED"
            )
            relations[name[:-5]], bodies[name] = values, body
        encoded = canonical_json(inventory) + ("\n" if venue == "polymarket" else "")
        require(
            sha256(encoded.encode()) == source_release_id, "producer release inventory mismatch"
        )
        _validate_rows(venue, relations, selected)
        if destination is not None:
            require(
                total <= limits.max_temp_bytes,
                "copy exceeds temporary byte limit",
                "LIMIT_EXCEEDED",
            )
            destination = Path(destination)
            parent = _directory(destination.parent)
            try:
                os.mkdir(destination.name, mode=0o700, dir_fd=parent)
                out = os.open(
                    destination.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
                )
            finally:
                os.close(parent)
            for name, body in {**bodies, "manifest.json": manifest_body}.items():
                child = os.open(
                    name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=out
                )
                with os.fdopen(child, "wb") as stream:
                    stream.write(body)
            root = destination
        return VerifiedBundle(
            venue,
            Path(root),
            manifest,
            expected_manifest_sha256,
            relations,
            tuple(sorted(selected)),
        )
    except OSError as error:
        raise SemanticsError("INVALID_INPUT", "unsafe or unavailable bundle filesystem") from error
    finally:
        if out is not None:
            os.close(out)
        if fd is not None:
            os.close(fd)


def load_bundle(
    venue,
    source,
    destination,
    expected_manifest_sha256,
    source_release_id,
    selected_market_ids,
    limits=Limits(),
):
    return _bundle(
        venue,
        source,
        expected_manifest_sha256,
        source_release_id,
        selected_market_ids,
        limits,
        destination,
    )


def verify_bundle(
    venue, root, expected_manifest_sha256, source_release_id, selected_market_ids, limits=Limits()
):
    return _bundle(
        venue, root, expected_manifest_sha256, source_release_id, selected_market_ids, limits
    )


def read_regular(path: Path, max_bytes: int) -> bytes:
    """Read one bounded regular file through non-following directory descriptors."""
    fd = None
    try:
        path = Path(path)
        fd = _directory(path.parent)
        return _read(fd, path.name, max_bytes)
    except OSError as error:
        raise SemanticsError("INVALID_INPUT", "unsafe or unavailable regular file") from error
    finally:
        if fd is not None:
            os.close(fd)
