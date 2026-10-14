import runpy
import stat
import zipfile
from pathlib import Path

import pytest

AUDIT = runpy.run_path(str(Path(__file__).parents[1] / "scripts/check-distributions.py"))["main"]


@pytest.mark.parametrize(
    "name,body,mode",
    [
        ("data/live.json", b"{}", 0),
        ("../escape", b"", 0),
        ("src/link", b"outside", stat.S_IFLNK | 0o777),
        ("src/secret", b"-----BEGIN " + b"PRIVATE KEY-----", 0),
    ],
)
def test_distribution_audit_refuses_private_or_unsafe_members(
    tmp_path, monkeypatch, name, body, mode
):
    monkeypatch.chdir(tmp_path)
    Path("dist").mkdir()
    with zipfile.ZipFile("dist/example.whl", "w") as wheel:
        entry = zipfile.ZipInfo(name)
        entry.external_attr = mode << 16
        wheel.writestr(entry, body)
    with pytest.raises(SystemExit):
        AUDIT()


def test_distribution_audit_accepts_public_source(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    Path("dist").mkdir()
    with zipfile.ZipFile("dist/example.whl", "w") as wheel:
        wheel.writestr("market_semantics/__init__.py", b"__version__ = '0.1.0'")
    AUDIT()
    assert "verified 1 public distributions" in capsys.readouterr().out
