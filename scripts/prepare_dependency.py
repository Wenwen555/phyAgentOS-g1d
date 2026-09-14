#!/usr/bin/env python3
"""Fetch the pinned upstream source dependency; never install or start a robot Skill."""

import argparse
import hashlib
import io
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URL = "https://codeload.github.com/Forgelab-Robotics/adapter-forge-gateway/tar.gz/refs/tags/v1.0.2"
SHA256 = "8fbd3f677608bc1c79cb2c0c5e6116e80f9d7ea26dde5b4ef7c60b307705322a"


def prepare(archive: Path | None = None) -> Path:
    target = ROOT / ".deps" / "forge-gateway"
    if archive is None:
        with urllib.request.urlopen(URL, timeout=30) as response:
            data = response.read(4 * 1024 * 1024 + 1)
    else:
        data = archive.read_bytes()
    if hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError("upstream archive SHA-256 mismatch")
    target.parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as directory:
        staging = Path(directory) / "source"
        staging.mkdir()
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            for member in tar:
                path = Path(member.name)
                if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
                    raise ValueError("unsafe upstream archive member")
                if not member.isfile():
                    continue
                relative = Path(*path.parts[1:])
                destination = staging / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(tar.extractfile(member).read())
        if target.exists():
            expected = {
                p.relative_to(staging): p.read_bytes() for p in staging.rglob("*") if p.is_file()
            }
            if any(
                not (target / p).is_file() or (target / p).read_bytes() != data
                for p, data in expected.items()
            ):
                raise RuntimeError(
                    "existing upstream dependency differs; use a clean .deps directory"
                )
        else:
            shutil.move(str(staging), target)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive", type=Path, help="Already downloaded, checksum-verified source archive"
    )
    print(prepare(parser.parse_args().archive))
