"""Build or verify the complete, reproducible inspector distribution."""
from __future__ import annotations
import argparse
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "screen_app_inspect.py", "inspect_support.py", "inspect_uia.py", "requirements.txt", "run.cmd",
    "verify.cmd", "README.txt", "CHANGELOG.txt", "CAPABILITIES.md", "tests/test_inspector.py",
    "tests/test_metadata_extensions.py", "tests/test_windows_smoke.py", "tests/native_target.py",
    "tools/build_package.py",
)


def payloads():
    result = {}
    for name in FILES:
        data = (ROOT / name).read_bytes().replace(b"\r\n", b"\n")
        if name.endswith(".cmd"):
            data = data.replace(b"\n", b"\r\n")
        if name.endswith(".py"):
            compile(data, name, "exec")
        result[name] = data
    return result


def build():
    stream = BytesIO()
    with ZipFile(stream, "w", compression=ZIP_DEFLATED) as archive:
        for name, data in payloads().items():
            entry = ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data)
    return stream.getvalue()


def verify(path):
    expected = payloads()
    with ZipFile(path) as archive:
        if archive.testzip() is not None or len(archive.namelist()) != len(expected) or set(archive.namelist()) != set(expected):
            raise ValueError("ZIP integrity or file inventory does not match this project")
        for name, data in expected.items():
            if archive.read(name) != data:
                raise ValueError(f"ZIP payload differs from project source: {name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/screen-app-inspect.zip")
    parser.add_argument("--check", type=Path)
    args = parser.parse_args()
    if args.check:
        verify(args.check)
        print("ZIP inventory, contents, and Python syntax verified.")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(build())
        verify(args.output)
        print(f"Built and verified: {args.output}")


if __name__ == "__main__":
    main()
