"""Audit built public artifacts without importing application code."""

import re
import stat
import tarfile
import zipfile
from pathlib import Path


def main():
    artifacts = sorted(Path("dist").glob("*.whl")) + sorted(Path("dist").glob("*.tar.gz"))
    if not artifacts:
        raise SystemExit("no built distributions")
    for artifact in artifacts:
        if artifact.suffix == ".whl":
            with zipfile.ZipFile(artifact) as archive:
                if any(stat.S_ISLNK(item.external_attr >> 16) for item in archive.infolist()):
                    raise SystemExit("distribution contains links")
                names = archive.namelist()
                bodies = [archive.read(name) for name in names if not name.endswith("/")]
        else:
            with tarfile.open(artifact) as archive:
                members = archive.getmembers()
                if any(item.issym() or item.islnk() for item in members):
                    raise SystemExit("distribution contains links")
                names = [item.name for item in members]
                bodies = [archive.extractfile(item).read() for item in members if item.isfile()]
        if len(names) != len(set(names)):
            raise SystemExit("distribution contains duplicate members")
        for body in bodies:
            if re.search(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", body) or re.search(
                rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", body
            ):
                raise SystemExit("distribution contains possible credential material")
        for name in names:
            parts = Path(name).parts
            if (
                name.startswith("/")
                or ".." in parts
                or any(
                    part
                    in {"data", ".pad", ".git", ".venv", "__pycache__", "credentials.json", ".env"}
                    for part in parts
                )
                or name.endswith((".db", ".sqlite", ".key", ".pyc"))
            ):
                raise SystemExit(f"forbidden distribution member: {name}")
    print(f"verified {len(artifacts)} public distributions")


if __name__ == "__main__":
    main()
