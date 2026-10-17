"""Exercise installed producer handoffs and an independently installed offline consumer."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

PRODUCERS = {
    "polymarket": (
        "POLYMARKET_EVENTS_SOURCE",
        "polymarket/polymarket-events",
        "10cfedce999e41fc63459b9054fb611b5b3f0297",
        "catalogue",
    ),
    "kalshi": (
        "KALSHI_EVENTS_SOURCE",
        "kalshi/kalshi-events",
        "6fb46f7cf2bd6e9e5c3c750d71a68188f24e0872",
        "kalshi-events",
    ),
}
ELECTION = "election:us:president:2024:general"
NETWORK_GUARD = """\
import sys
import importlib.abc

class NoProducers(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'oddsfox_catalogue', 'kalshi_events',
                                     'oddsfox_graph', 'oddsgraph', 'hypertrial'}:
            raise ImportError('producer/Graph import forbidden: ' + fullname)

sys.meta_path.insert(0, NoProducers())

def deny_network(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        import os
        path = os.path.abspath(os.fsdecode(args[0]))
        if any(path == root or path.startswith(root + os.sep) for root in BLOCKED_ROOTS):
            raise RuntimeError('producer source access forbidden')
    if event == 'socket.getaddrinfo':
        raise RuntimeError('consumer network forbidden')
    if event == 'socket.connect' and args[0].family in (2, 10):
        raise RuntimeError('consumer network forbidden')

sys.addaudithook(deny_network)
"""


def run(command, *, cwd, env):
    result = subprocess.run(
        [str(x) for x in command],
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=240,
    )
    if result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {command}\n{result.stdout}\n{result.stderr}"
        )
    return result.stdout


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def pinned_producer(venue, repo, env):
    expected = PRODUCERS[venue][2]
    actual = run(["git", "rev-parse", "HEAD"], cwd=repo, env=env).strip()
    if actual != expected:
        raise RuntimeError(f"{venue}: expected producer revision {expected}, got {actual}")
    if run(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=repo, env=env):
        raise RuntimeError(f"{venue}: pinned producer checkout must be clean")


def install(wheel, environment, unrelated, env):
    run(["uv", "venv", "--python", "3.12", environment], cwd=unrelated, env=env)
    python = environment / "bin/python"
    run(["uv", "pip", "install", "--python", python, wheel], cwd=unrelated, env=env)
    return python


def responses():
    poly = {
        "id": "1",
        "version": "v1",
        "conditionId": "0x" + "a" * 64,
        "question": "Will Donald Trump win the 2024 US Presidential Election?",
        "description": "Resolves Yes if Donald Trump wins the 2024 US presidential election.",
        "outcomes": '["Yes","No"]',
        "clobTokenIds": '["101","102"]',
    }
    market = {
        "ticker": "PRES-2024-DJT",
        "event_ticker": "PRES-2024",
        "title": "Will Donald Trump win the 2024 US Presidential Election?",
        "yes_sub_title": "Donald Trump",
        "no_sub_title": "Not Donald Trump",
        "rules_primary": "Resolves Yes if Donald Trump wins the 2024 US presidential election.",
        "rules_secondary": "",
        "market_type": "binary",
        "yes_bid": 50,
    }
    return {
        "/markets/1": (200, poly),
        "/trade-api/v2/historical/cutoff": (
            200,
            {
                "market_settled_ts": "2025-01-01T00:00:00Z",
                "trades_created_ts": "2025-01-01T00:00:00Z",
            },
        ),
        "/trade-api/v2/markets/PRES-2024-DJT": (404, {}),
        "/trade-api/v2/historical/markets/PRES-2024-DJT": (200, {"market": market}),
        "/trade-api/v2/events/PRES-2024": (
            200,
            {
                "event": {
                    "event_ticker": "PRES-2024",
                    "series_ticker": "PRES",
                    "title": "2024 US Presidential Election",
                    "sub_title": "General election",
                }
            },
        ),
        "/trade-api/v2/series/PRES": (
            200,
            {
                "series": {
                    "ticker": "PRES",
                    "title": "US Presidential Election",
                    "frequency": "custom",
                }
            },
        ),
    }


def acquire(root, environments, unrelated, env):
    expected = responses()
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = urlsplit(self.path).path
            seen.append(path)
            status, payload = expected.get(path, (500, {"error": "unexpected request"}))
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    receipts = {}
    try:
        poly_env = {
            **env,
            "CATALOGUE_ROOT": str(root / "polymarket-source"),
            "CATALOGUE_GAMMA_BASE_URL": origin,
            "CATALOGUE_GAMMA_ALLOW_LOOPBACK": "true",
        }
        receipts["polymarket"] = json.loads(
            run(
                [
                    environments["polymarket"] / "bin/catalogue",
                    "metadata",
                    "refresh",
                    "--market-id",
                    "1",
                    "--output",
                    root / "polymarket-bundle",
                    "--max-requests",
                    "4",
                    "--max-download-bytes",
                    "1048576",
                    "--max-response-bytes",
                    "262144",
                    "--max-output-bytes",
                    "2097152",
                ],
                cwd=unrelated,
                env=poly_env,
            )
        )
        kalshi_env = {
            **env,
            "KALSHI_EVENTS_ROOT": str(root / "kalshi-source"),
            "KALSHI_EVENTS_API_URL": origin + "/trade-api/v2",
            "KALSHI_EVENTS_ALLOW_LOOPBACK": "1",
        }
        receipts["kalshi"] = json.loads(
            run(
                [
                    environments["kalshi"] / "bin/kalshi-events",
                    "metadata",
                    "refresh",
                    "--market-ticker",
                    "PRES-2024-DJT",
                    "--output",
                    root / "kalshi-bundle",
                    "--max-requests",
                    "8",
                    "--max-download-bytes",
                    "1048576",
                    "--max-response-bytes",
                    "262144",
                    "--max-duration-seconds",
                    "30",
                    "--max-output-bytes",
                    "2097152",
                ],
                cwd=unrelated,
                env=kalshi_env,
            )
        )
        run(
            [
                environments["kalshi"] / "bin/kalshi-events",
                "metadata",
                "verify",
                "--output",
                root / "kalshi-bundle",
            ],
            cwd=unrelated,
            env=env,
        )
        assert receipts["polymarket"]["http_attempts"] == 1
        assert receipts["polymarket"]["found"] == 1
        assert receipts["kalshi"]["requests"] == 5
        assert receipts["kalshi"]["accounting_complete"] is True
        assert sorted(seen) == sorted(expected), "unexpected or missing producer requests"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return receipts


def main():
    checkout = Path(__file__).resolve().parents[1]
    wheels = sorted((checkout / "dist").glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit("expected exactly one built consumer wheel")
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"}
        and not key.startswith(("CATALOGUE_", "KALSHI_EVENTS_"))
    }
    repositories = {
        venue: Path(os.environ.get(spec[0], checkout.parent / spec[1])).resolve()
        for venue, spec in PRODUCERS.items()
    }
    for venue, repo in repositories.items():
        pinned_producer(venue, repo, env)
    with tempfile.TemporaryDirectory(prefix="market-semantics-installed-") as directory:
        root = Path(directory).resolve()
        unrelated = root / "unrelated"
        unrelated.mkdir()
        environments = {}
        for venue, repo in repositories.items():
            source = root / f"{venue}-build-source"
            source.mkdir()
            archive = root / f"{venue}.tar"
            run(
                ["git", "archive", "--format=tar", "--output", archive, PRODUCERS[venue][2]],
                cwd=repo,
                env=env,
            )
            with tarfile.open(archive) as retained:
                retained.extractall(source, filter="data")
            archive.unlink()
            wheel_root = root / f"{venue}-wheels"
            run(["uv", "build", "--wheel", "--out-dir", wheel_root, source], cwd=unrelated, env=env)
            producer_wheels = list(wheel_root.glob("*.whl"))
            assert len(producer_wheels) == 1
            environment = root / f"{venue}-venv"
            python = install(producer_wheels[0], environment, unrelated, env)
            module = "oddsfox_catalogue" if venue == "polymarket" else "kalshi_events"
            run(
                [python, "-c", f"import {module}; assert 'site-packages' in {module}.__file__"],
                cwd=unrelated,
                env=env,
            )
            environments[venue] = environment
        consumer_env = root / "consumer-venv"
        consumer_python = install(wheels[0], consumer_env, unrelated, env)
        receipts = acquire(root, environments, unrelated, env)
        inputs = {}
        for venue in PRODUCERS:
            bundle = root / f"{venue}-bundle"
            body = (bundle / "manifest.json").read_bytes()
            manifest = json.loads(body)
            assert hashlib.sha256(body).hexdigest() == receipts[venue]["manifest_sha256"]
            assert manifest["source_release_id"] == receipts[venue]["source_release_id"]
            inputs[venue] = {
                "path": str(bundle),
                "contract": manifest["contract"],
                "source_release_id": manifest["source_release_id"],
                "manifest_sha256": hashlib.sha256(body).hexdigest(),
                "selected_market_ids": ["1" if venue == "polymarket" else "PRES-2024-DJT"],
            }
            shutil.rmtree(root / f"{venue}-source")
            shutil.rmtree(environments[venue])
            shutil.rmtree(root / f"{venue}-build-source")
            assert not (root / f"{venue}-source").exists()
        write_json(root / "producer-receipts.json", receipts)
        curation = root / "curation.json"
        write_json(
            curation,
            {
                "version": 1,
                "election_id": ELECTION,
                "decisions": [],
                "supersedes": {"mappings": {}, "relationships": {}},
            },
        )
        selection = root / "selection.json"
        write_json(
            selection,
            {
                "version": 1,
                "election_id": ELECTION,
                "inputs": inputs,
                "curation": {
                    "path": str(curation),
                    "sha256": hashlib.sha256(curation.read_bytes()).hexdigest(),
                },
                "limits": {},
            },
        )
        site = Path(
            run(
                [consumer_python, "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
                cwd=unrelated,
                env=env,
            ).strip()
        )
        (site / "market_semantics_acceptance_guard.py").write_text(
            "BLOCKED_ROOTS = "
            + repr([str(p) for p in repositories.values()])
            + "\n"
            + NETWORK_GUARD
        )
        (site / "market_semantics_acceptance_guard.pth").write_text(
            "import market_semantics_acceptance_guard\n"
        )
        # A pre-existing startup module must not shadow the acceptance guard.
        existing_startup = root / "existing-startup"
        existing_startup.mkdir()
        (existing_startup / "sitecustomize.py").write_text("ACCEPTANCE_PREEXISTING = True\n")
        (site / "00_acceptance_existing_startup.pth").write_text(
            "import sys; sys.path.insert(0, " + repr(str(existing_startup)) + ")\n"
        )
        run(
            [
                consumer_python,
                "-c",
                "import market_semantics,sitecustomize,market_semantics_acceptance_guard; "
                "assert 'site-packages' in market_semantics.__file__; "
                "assert sitecustomize.ACCEPTANCE_PREEXISTING; "
                "assert 'site-packages' in market_semantics_acceptance_guard.__file__",
            ],
            cwd=unrelated,
            env=env,
        )
        guard_check = """
import importlib, socket, sitecustomize, market_semantics_acceptance_guard
assert sitecustomize.ACCEPTANCE_PREEXISTING
assert 'site-packages' in market_semantics_acceptance_guard.__file__
for root in market_semantics_acceptance_guard.BLOCKED_ROOTS:
    try:
        open(root + '/pyproject.toml')
    except RuntimeError as error:
        assert 'source access forbidden' in str(error)
    else:
        raise AssertionError('original producer checkout accessible')
for name in ('oddsfox_catalogue', 'kalshi_events', 'oddsfox_graph', 'oddsgraph', 'hypertrial'):
    try:
        importlib.import_module(name)
    except ImportError as error:
        assert 'forbidden' in str(error)
    else:
        raise AssertionError(name)
try:
    socket.create_connection(('127.0.0.1', 9))
except RuntimeError as error:
    assert 'network forbidden' in str(error)
else:
    raise AssertionError('network guard missing')
"""
        run([consumer_python, "-c", guard_check], cwd=unrelated, env=env)
        child_probe = unrelated / "guard-child.py"
        child_probe.write_text(
            "import multiprocessing\n"
            + "def probe():\n    exec("
            + repr(guard_check)
            + ")\n"
            + "if __name__ == '__main__':\n"
            + "    child = multiprocessing.get_context('spawn').Process(target=probe)\n"
            + "    child.start()\n    child.join(30)\n"
            + "    assert child.exitcode == 0, 'spawned child guard failed'\n"
        )
        run([consumer_python, child_probe], cwd=unrelated, env=env)
        cli = consumer_env / "bin/market-semantics"
        release = root / "release"
        compiled = json.loads(
            run(
                [cli, "compile", "--selection", selection, "--output", release],
                cwd=unrelated,
                env=env,
            )
        )
        pin = compiled["manifest_sha256"]
        verified = json.loads(
            run(
                [cli, "verify", "--release", release, "--expected-manifest-sha256", pin],
                cwd=unrelated,
                env=env,
            )
        )
        assert verified["release_id"] == compiled["release_id"]
        # Verification must use retained evidence after all acquisition paths disappear.
        for venue in PRODUCERS:
            shutil.rmtree(root / f"{venue}-bundle")
        selection.unlink()
        curation.unlink()
        copied = root / "copied-release"
        shutil.copytree(release, copied)
        shutil.rmtree(release)
        copied_result = json.loads(
            run(
                [cli, "verify", "--release", copied, "--expected-manifest-sha256", pin],
                cwd=unrelated,
                env=env,
            )
        )
        assert copied_result["release_id"] == compiled["release_id"]
        run(
            [
                consumer_python,
                "-c",
                "import pyarrow.parquet as pq; from pathlib import Path; "
                + "root=Path("
                + repr(str(copied))
                + "); "
                + "native=pq.read_table(root/'native_outcomes.parquet').to_pylist(); "
                + "assert len(native)==4; "
                + "assert {r['venue'] for r in native}=={'polymarket','kalshi'}; "
                + "assert pq.read_table(root/'coverage.parquet').num_rows==2",
            ],
            cwd=unrelated,
            env=env,
        )
    print("verified pinned installed producers -> offline installed consumer -> copied release")


if __name__ == "__main__":
    main()
