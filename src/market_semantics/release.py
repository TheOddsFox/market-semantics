"""Deterministic staging and independent, bounded offline release verification."""

from __future__ import annotations

import os
import platform
import re
from dataclasses import asdict
from pathlib import Path, PurePosixPath

import pyarrow as pa
import pyarrow.parquet as pq

from market_semantics import __version__
from market_semantics.contracts import (
    CONTRACT,
    ELECTION_ID,
    MAX_DECODED_BATCH_BYTES,
    MAX_INVENTORY_FILES,
    MAX_MANIFEST_BYTES,
    RULE_REVISION,
    SCHEMAS,
    SORT_KEYS,
    VENUE_CONTRACTS,
    Limits,
    PreparedRelease,
    SemanticsError,
    identity,
    json_bytes,
    require,
    sha256,
    strict_json,
)
from market_semantics.inputs import read_regular
from market_semantics.validation import validate_tables

ROW_GROUP_SIZE = 1024


def _read(root: Path, name: str, maximum: int) -> bytes:
    """Read literal regular files without following a symlink in any component."""
    path = PurePosixPath(name)
    require(
        not path.is_absolute()
        and path.parts
        and all(part not in {".", ".."} for part in path.parts)
        and str(path) == name,
        "invalid release filename",
    )
    return read_regular(root / name, maximum)


def _files(root: Path) -> list[str]:
    require(root.is_dir() and not root.is_symlink(), "release root must be a regular directory")
    names = []
    entry_count = 0
    for directory, directories, files in os.walk(root, followlinks=False):
        entry_count += len(directories) + len(files)
        require(
            entry_count <= MAX_INVENTORY_FILES + 8, "too many release entries", "LIMIT_EXCEEDED"
        )
        require(
            len(Path(directory).relative_to(root).parts) <= 2,
            "release directory nesting exceeds limit",
            "LIMIT_EXCEEDED",
        )
        for name in directories:
            require(not (Path(directory) / name).is_symlink(), "symlink directory in release")
        for name in files:
            path = Path(directory) / name
            require(not path.is_symlink() and path.is_file(), "nonregular release file")
            names.append(path.relative_to(root).as_posix())
            require(
                len(names) <= MAX_INVENTORY_FILES + 1, "too many release files", "LIMIT_EXCEEDED"
            )
    return sorted(names)


def _schema(name: str) -> list[dict]:
    return [
        {"name": field.name, "type": str(field.type), "nullable": field.nullable}
        for field in SCHEMAS[name]
    ]


def _sorted_table(name: str, rows: list[dict]) -> pa.Table:
    table = pa.Table.from_pylist(rows, schema=SCHEMAS[name])
    if table.num_rows:
        table = table.sort_by([(key, "ascending") for key in SORT_KEYS[name]])
    return table


def _selection(
    selection: dict, bundles: dict, curation: dict, limits: Limits, curation_bytes: bytes
) -> dict:
    require(
        isinstance(selection, dict)
        and set(selection) == {"version", "election_id", "inputs", "curation", "limits"},
        "invalid selection fields",
    )
    require(
        type(selection["version"]) is int
        and selection["version"] == 1
        and selection["election_id"] == ELECTION_ID,
        "unsupported selection",
    )
    require(
        isinstance(selection["inputs"], dict) and set(selection["inputs"]) == set(VENUE_CONTRACTS),
        "both inputs required",
    )
    require(
        isinstance(selection["curation"], dict)
        and set(selection["curation"]) == {"path", "sha256"},
        "invalid curation selection",
    )
    require(
        selection["curation"]["sha256"] == sha256(curation_bytes),
        "curation pin differs from retained curation",
    )
    require(
        asdict(Limits.from_dict(selection["limits"])) == asdict(limits), "selection limits differ"
    )
    sources = {}
    for venue, bundle in sorted(bundles.items()):
        source = selection["inputs"][venue]
        require(
            isinstance(source, dict)
            and set(source)
            == {"path", "contract", "source_release_id", "manifest_sha256", "selected_market_ids"},
            "invalid input selection",
        )
        require(
            isinstance(source["selected_market_ids"], list)
            and all(isinstance(value, str) and value for value in source["selected_market_ids"])
            and source["contract"] == VENUE_CONTRACTS[venue]
            and source["source_release_id"] == bundle.source_release_id
            and source["manifest_sha256"] == bundle.manifest_sha256
            and sorted(source["selected_market_ids"]) == list(bundle.selected_market_ids),
            "selection differs from verified bundle",
        )
        sources[venue] = {
            **source,
            "path": f"inputs/{venue}",
            "selected_market_ids": sorted(source["selected_market_ids"]),
        }
    return {
        "version": 1,
        "election_id": ELECTION_ID,
        "inputs": sources,
        "curation": {"path": "curation.json", "sha256": selection["curation"]["sha256"]},
        "limits": asdict(limits),
    }


def _runtime() -> dict:
    package = Path(__file__).parent
    build = strict_json((package / "build.json").read_bytes())
    require(
        isinstance(build, dict)
        and isinstance(build.get("lock_sha256"), str)
        and len(build["lock_sha256"]) == 64,
        "invalid packaged build identity",
    )
    sources = {
        path.relative_to(package).as_posix(): sha256(path.read_bytes())
        for path in sorted(package.rglob("*.py"))
    }
    return {
        "package_version": __version__,
        "python_version": platform.python_version(),
        "pyarrow_version": pa.__version__,
        "lock_sha256": build["lock_sha256"],
        "source_revision": identity("compiler", sources),
    }


def _manifest(inventory: dict, selection: dict, curation: dict, compiler: dict) -> dict:
    body = {
        "contract": CONTRACT,
        "schema_version": 1,
        "compiler": compiler,
        "rule_revision": RULE_REVISION,
        "curation_id": identity("curation", curation),
        "selection": selection,
        "coverage_scope": "bounded_selection",
        "inventory": inventory,
    }
    return {**body, "release_id": identity("release", body)}


def _inventory(root: Path, tables: dict, limits: Limits) -> dict:
    inventory = {}
    names = [name for name in _files(root) if name != "manifest.json"]
    sizes = {name: (root / name).stat(follow_symlinks=False).st_size for name in names}
    require(
        sum(sizes.values()) <= min(limits.max_release_bytes, limits.max_temp_bytes),
        "release exceeds total byte limit",
        "LIMIT_EXCEEDED",
    )
    for name in names:
        data = _read(root, name, sizes[name])
        require(len(data) == sizes[name], "staging file changed during inventory")
        item = {"size_bytes": len(data), "sha256": sha256(data)}
        if name.endswith(".parquet") and name.removesuffix(".parquet") in SCHEMAS:
            relation = name.removesuffix(".parquet")
            item.update({"row_count": len(tables[relation]), "schema": _schema(relation)})
        inventory[name] = item
    return inventory


def _write_artifact(stage: Path, name: str, data: bytes, total: int, limits: Limits) -> int:
    total += len(data)
    require(
        total <= min(limits.max_release_bytes, limits.max_temp_bytes),
        "release exceeds total byte limit",
        "LIMIT_EXCEEDED",
    )
    # Check the aggregate budget before creating or extending a staging artifact.
    with (stage / name).open("xb") as stream:
        stream.write(data)
    return total


def prepare_release(tables, bundles, curation, selection, stage, limits):
    stage = Path(stage)
    require(stage.is_dir() and not stage.is_symlink(), "staging root must exist")
    from market_semantics.inputs import verify_bundle

    require(set(bundles) == set(VENUE_CONTRACTS), "both verified bundles required")
    for venue, bundle in bundles.items():
        require(
            bundle.root.resolve() == (stage / "inputs" / venue).resolve(),
            "bundle must be retained in staging inputs",
        )
    require(
        all(name.startswith("inputs/") or name == "curation.json" for name in _files(stage)),
        "staging outputs already exist",
    )
    bundles = {
        venue: verify_bundle(
            venue,
            bundle.root,
            bundle.manifest_sha256,
            bundle.source_release_id,
            bundle.selected_market_ids,
            limits,
        )
        for venue, bundle in bundles.items()
    }
    report = validate_tables(tables, bundles, curation, limits)
    curation_bytes = _read(stage, "curation.json", MAX_MANIFEST_BYTES)
    require(strict_json(curation_bytes) == curation, "retained curation differs")
    retained_selection = _selection(selection, bundles, curation, limits, curation_bytes)
    initial_inventory = _inventory(stage, tables, limits)
    total = sum(item["size_bytes"] for item in initial_inventory.values())
    for name, rows in tables.items():
        table = _sorted_table(name, rows)
        require(
            table.nbytes <= limits.max_release_bytes, "decoded table too large", "LIMIT_EXCEEDED"
        )
        for batch in table.to_batches(max_chunksize=ROW_GROUP_SIZE):
            require(
                batch.nbytes <= MAX_DECODED_BATCH_BYTES, "decoded batch too large", "LIMIT_EXCEEDED"
            )
        sink = pa.BufferOutputStream()
        pq.write_table(
            table,
            sink,
            version="2.6",
            compression="NONE",
            use_dictionary=False,
            row_group_size=ROW_GROUP_SIZE,
            write_statistics=True,
            data_page_version="1.0",
        )
        total = _write_artifact(
            stage, f"{name}.parquet", sink.getvalue().to_pybytes(), total, limits
        )
    for name, document in (
        ("selection.json", retained_selection),
        ("validation_report.json", report),
    ):
        data = json_bytes(document)
        require(len(data) <= MAX_MANIFEST_BYTES, "release JSON too large", "LIMIT_EXCEEDED")
        total = _write_artifact(stage, name, data, total, limits)
    manifest = _manifest(
        _inventory(stage, tables, limits), retained_selection, curation, _runtime()
    )
    data = json_bytes(manifest)
    require(len(data) <= MAX_MANIFEST_BYTES, "manifest too large", "LIMIT_EXCEEDED")
    require(
        sum(item["size_bytes"] for item in manifest["inventory"].values()) + len(data)
        <= min(limits.max_release_bytes, limits.max_temp_bytes),
        "release exceeds total byte limit",
        "LIMIT_EXCEEDED",
    )
    _write_artifact(stage, "manifest.json", data, total, limits)
    return PreparedRelease(stage, manifest["release_id"], sha256(data), report)


def _read_parquet(data: bytes, name: str, item: dict, limits: Limits) -> list[dict]:
    require(
        type(item.get("row_count")) is int
        and 0 <= item["row_count"] <= limits.max_records_per_bundle,
        "invalid Parquet row count",
        "LIMIT_EXCEEDED",
    )
    try:
        parquet = pq.ParquetFile(
            pa.BufferReader(data),
            thrift_string_size_limit=MAX_MANIFEST_BYTES,
            thrift_container_size_limit=MAX_INVENTORY_FILES * 1024,
        )
        require(
            parquet.schema_arrow.equals(SCHEMAS[name], check_metadata=True),
            f"schema mismatch: {name}",
            "VALIDATION_FAILED",
        )
        require(
            type(item["row_count"]) is int
            and parquet.metadata.num_rows == item["row_count"]
            and 0 <= item["row_count"] <= limits.max_records_per_bundle,
            "invalid Parquet row count",
            "LIMIT_EXCEEDED",
        )
        require(
            parquet.metadata.num_row_groups <= limits.max_records_per_bundle,
            "too many row groups",
            "LIMIT_EXCEEDED",
        )
        total = 0
        for group in range(parquet.metadata.num_row_groups):
            metadata = parquet.metadata.row_group(group)
            require(metadata.num_rows <= ROW_GROUP_SIZE, "oversized row group", "LIMIT_EXCEEDED")
            for column_index in range(metadata.num_columns):
                column = metadata.column(column_index)
                require(
                    column.compression == "UNCOMPRESSED"
                    and column.total_uncompressed_size <= MAX_DECODED_BATCH_BYTES
                    and column.total_compressed_size <= len(data),
                    "unsupported or oversized Parquet column",
                    "LIMIT_EXCEEDED",
                )
            require(
                metadata.total_byte_size <= MAX_DECODED_BATCH_BYTES,
                "decoded row group too large",
                "LIMIT_EXCEEDED",
            )
            total += metadata.total_byte_size
        require(total <= limits.max_release_bytes, "decoded relation too large", "LIMIT_EXCEEDED")
        rows = []
        for batch in parquet.iter_batches(batch_size=ROW_GROUP_SIZE):
            require(
                batch.nbytes <= MAX_DECODED_BATCH_BYTES, "decoded batch too large", "LIMIT_EXCEEDED"
            )
            rows.extend(batch.to_pylist())
        require(rows == _sorted_table(name, rows).to_pylist(), "unsorted relation")
        return rows
    except (pa.ArrowException, OSError, KeyError, TypeError) as error:
        raise SemanticsError("VALIDATION_FAILED", f"invalid Parquet relation: {name}") from error


def verify_release(root, expected_manifest_sha256, limits=Limits()):
    from market_semantics.inputs import verify_bundle

    root = Path(root)
    data = _read(root, "manifest.json", min(MAX_MANIFEST_BYTES, limits.max_release_bytes))
    require(sha256(data) == expected_manifest_sha256, "release manifest pin mismatch")
    manifest = strict_json(data)
    require(
        isinstance(manifest, dict)
        and set(manifest)
        == {
            "contract",
            "schema_version",
            "compiler",
            "rule_revision",
            "curation_id",
            "selection",
            "coverage_scope",
            "inventory",
            "release_id",
        },
        "invalid manifest fields",
    )
    require(
        manifest["contract"] == CONTRACT
        and type(manifest["schema_version"]) is int
        and manifest["schema_version"] == 1
        and manifest["rule_revision"] == RULE_REVISION
        and manifest["coverage_scope"] == "bounded_selection",
        "unsupported release contract",
    )
    compiler = manifest["compiler"]
    require(
        isinstance(compiler, dict)
        and set(compiler)
        == {
            "package_version",
            "python_version",
            "pyarrow_version",
            "lock_sha256",
            "source_revision",
        }
        and all(isinstance(value, str) and value for value in compiler.values()),
        "invalid compiler identity",
    )
    require(
        re.fullmatch(r"[0-9a-f]{64}", compiler["lock_sha256"])
        and re.fullmatch(r"compiler:[0-9a-f]{64}", compiler["source_revision"]),
        "invalid compiler digest",
    )
    inventory = manifest["inventory"]
    require(
        isinstance(inventory, dict) and len(inventory) <= MAX_INVENTORY_FILES,
        "invalid inventory size",
        "LIMIT_EXCEEDED",
    )
    require("manifest.json" not in inventory, "manifest must not inventory itself")
    require(set(_files(root)) == set(inventory) | {"manifest.json"}, "release inventory differs")
    contents = {}
    total = len(data)
    for name, item in inventory.items():
        require(
            isinstance(item, dict)
            and type(item.get("size_bytes")) is int
            and 0 <= item["size_bytes"] <= limits.max_release_bytes,
            "invalid inventory file size",
            "LIMIT_EXCEEDED",
        )
        expected_keys = (
            {"size_bytes", "sha256", "row_count", "schema"}
            if name in {f"{relation}.parquet" for relation in SCHEMAS}
            else {"size_bytes", "sha256"}
        )
        require(set(item) == expected_keys, "invalid inventory entry fields")
        total += item["size_bytes"]
        require(
            total <= min(limits.max_release_bytes, limits.max_temp_bytes),
            "release exceeds total byte limit",
            "LIMIT_EXCEEDED",
        )
        if name in {"curation.json", "selection.json", "validation_report.json"}:
            require(
                item["size_bytes"] <= MAX_MANIFEST_BYTES, "release JSON too large", "LIMIT_EXCEEDED"
            )
    for name, item in inventory.items():
        contents[name] = _read(root, name, item["size_bytes"])
        require(
            len(contents[name]) == item["size_bytes"]
            and sha256(contents[name]) == item.get("sha256"),
            "release file checksum mismatch",
        )
    required = {f"{name}.parquet" for name in SCHEMAS} | {
        "curation.json",
        "selection.json",
        "validation_report.json",
    }
    require(required <= set(contents), "missing release artifacts")
    require(
        all(name in required or name.startswith("inputs/") for name in contents),
        "unexpected release artifacts",
    )
    for name in ("curation.json", "selection.json", "validation_report.json"):
        require(
            len(contents[name]) <= MAX_MANIFEST_BYTES, "release JSON too large", "LIMIT_EXCEEDED"
        )
    curation = strict_json(contents["curation.json"])
    selection = strict_json(contents["selection.json"])
    require(
        isinstance(selection, dict) and isinstance(selection.get("inputs"), dict),
        "invalid retained selection",
    )
    recorded_limits = Limits.from_dict(selection.get("limits"))
    require(
        all(getattr(recorded_limits, key) <= getattr(limits, key) for key in asdict(limits)),
        "release limits exceed verifier limits",
        "LIMIT_EXCEEDED",
    )
    require(
        total <= min(recorded_limits.max_release_bytes, recorded_limits.max_temp_bytes),
        "release exceeds recorded total byte limit",
        "LIMIT_EXCEEDED",
    )
    bundles = {}
    require(set(selection["inputs"]) == set(VENUE_CONTRACTS), "both retained bundles required")
    for venue, source in selection["inputs"].items():
        require(
            isinstance(source, dict)
            and set(source)
            == {"path", "contract", "source_release_id", "manifest_sha256", "selected_market_ids"}
            and source.get("path") == f"inputs/{venue}",
            "invalid retained bundle path",
        )
        bundles[venue] = verify_bundle(
            venue,
            root / "inputs" / venue,
            source["manifest_sha256"],
            source["source_release_id"],
            source["selected_market_ids"],
            recorded_limits,
        )
    require(
        _selection(selection, bundles, curation, recorded_limits, contents["curation.json"])
        == selection,
        "retained selection is not canonical",
    )
    tables = {}
    for name in SCHEMAS:
        item = inventory[f"{name}.parquet"]
        require(
            set(item) == {"size_bytes", "sha256", "row_count", "schema"}
            and item["schema"] == _schema(name),
            "invalid relation inventory",
        )
        tables[name] = _read_parquet(contents[f"{name}.parquet"], name, item, recorded_limits)
    report = validate_tables(tables, bundles, curation, recorded_limits)
    require(
        strict_json(contents["validation_report.json"]) == report,
        "validation report differs from recomputed invariants",
        "VALIDATION_FAILED",
    )
    require(
        _manifest(inventory, selection, curation, manifest["compiler"]) == manifest,
        "release identity differs",
        "VALIDATION_FAILED",
    )
    return PreparedRelease(root, manifest["release_id"], sha256(data), report)
