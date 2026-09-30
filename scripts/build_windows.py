#!/usr/bin/env python3
"""Build the Windows zip: official embeddable Python + tzdata + the app + installer scripts.

    python3 scripts/build_windows.py            -> dist/DownloadWatch-<version>-windows-x64.zip

Downloads are pinned and checked against SHA-256 below; bump them together.
"""
import hashlib
import io
import os
import re
import sys
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON_VERSION = "3.13.15"
PYTHON_URL = f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip"
PYTHON_SHA256 = "d1f04d990aee1253d8569e8e5104e30fa9f5fa830899f14843448872d936a2cf"
TZDATA_URL = ("https://files.pythonhosted.org/packages/f9/bc/8737e8d54cf51106118039b83f485a4783112fab49ea9d044b234978a46e/"
              "tzdata-2026.4-py2.py3-none-any.whl")
TZDATA_SHA256 = "c2169a8b0a7a5e9674da5a135ccdfb2b3e671b333ed9fed17b41f73c34476e81"


def fetch(url, sha256):
    cache = os.path.join(ROOT, "dist", "cache", os.path.basename(url))
    if os.path.exists(cache):
        data = open(cache, "rb").read()
    else:
        with urllib.request.urlopen(url, timeout=120) as r:
            data = r.read()
    got = hashlib.sha256(data).hexdigest()
    if got != sha256:
        sys.exit(f"checksum mismatch for {url}: {got}")
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    with open(cache, "wb") as f:
        f.write(data)
    return data


def main():
    src = open(os.path.join(ROOT, "app", "downloadwatch.py"), encoding="utf-8").read()
    version = re.search(r'^__version__ = "([^"]+)"', src, re.M).group(1)
    top = f"DownloadWatch-{version}-windows-x64"
    out = os.path.join(ROOT, "dist", top + ".zip")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    py = zipfile.ZipFile(io.BytesIO(fetch(PYTHON_URL, PYTHON_SHA256)))
    tz = zipfile.ZipFile(io.BytesIO(fetch(TZDATA_URL, TZDATA_SHA256)))
    pth = f"python{PYTHON_VERSION.replace('.', '')[:3]}._pth"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        def add(name, data):
            info = zipfile.ZipInfo(f"{top}/{name}", date_time=(2026, 1, 1, 0, 0, 0))  # reproducible
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, data)
        for name in py.namelist():
            data = py.read(name)
            if name == pth:  # search path: stdlib zip, python dir, the app, bundled packages (tzdata)
                data = b"python313.zip\r\n.\r\n..\\app\r\n..\\lib\r\n"
            add(f"python/{name}", data)
        for name in tz.namelist():
            if name.startswith("tzdata/"):
                add(f"lib/{name}", tz.read(name))
            elif name.endswith("LICENSE") or name.endswith("licenses/LICENSE_APACHE"):
                add(f"lib/tzdata-LICENSE-{os.path.basename(name)}.txt", tz.read(name))
        for base, _, files in os.walk(os.path.join(ROOT, "app")):
            for f in files:
                if "__pycache__" in base or f.endswith(".pyc"):
                    continue
                full = os.path.join(base, f)
                add(os.path.relpath(full, ROOT).replace(os.sep, "/"), open(full, "rb").read())
        for f in os.listdir(os.path.join(ROOT, "windows")):
            add(f, open(os.path.join(ROOT, "windows", f), "rb").read())
        lic = open(os.path.join(ROOT, "LICENSE"), "rb").read().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        add("LICENSE.txt", lic)
    digest = hashlib.sha256(open(out, "rb").read()).hexdigest()
    with open(out + ".sha256", "w") as f:
        f.write(f"{digest}  {os.path.basename(out)}\n")
    print(out, os.path.getsize(out), digest)


if __name__ == "__main__":
    main()
