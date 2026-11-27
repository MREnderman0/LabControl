"""Manage the per-user Windows signing key and sign a verified release installer."""

import argparse
import base64
import ctypes
import hashlib
import json
import os
from ctypes import wintypes
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from lab_control.deployment.software_update import PUBLIC_KEY, VERSION, Release, manifest_message


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def protect(data, *, decrypt=False):
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    source, result = Blob(len(data), buffer), Blob()
    api = ctypes.windll.crypt32
    call = api.CryptUnprotectData if decrypt else api.CryptProtectData
    if not call(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.windll.kernel32.LocalFree(result.data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--init-key", action="store_true")
    parser.add_argument("--installer", type=Path)
    parser.add_argument("--notes", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    secret = Path(os.environ["LOCALAPPDATA"]) / "LabControlPublisher" / "graphite.key.dpapi"
    if args.init_key:
        secret.parent.mkdir(parents=True, exist_ok=True)
        if not secret.exists():
            key = Ed25519PrivateKey.generate()
            with secret.open("xb") as stream:
                stream.write(protect(key.private_bytes_raw()))
    key = Ed25519PrivateKey.from_private_bytes(protect(secret.read_bytes(), decrypt=True))
    public = key.public_key().public_bytes_raw().hex()
    if args.init_key:
        print(public)
        return
    if not (args.installer and args.notes and args.output) or public != PUBLIC_KEY:
        parser.error(
            "Provide installer, UTF-8 release notes and output; key must match the pinned desktop key"
        )
    if args.installer.name != Release(VERSION, "", 0, "").filename:
        parser.error("Installer name must match the desktop release")
    with args.installer.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    payload = dict(
        schema=1,
        target="windows-x64",
        version=VERSION,
        notes=args.notes.read_text("utf-8"),
        bytes=args.installer.stat().st_size,
        sha256=digest,
    )
    signature = base64.b64encode(key.sign(manifest_message(payload))).decode("ascii")
    args.output.write_text(
        json.dumps(dict(release=payload, signature=signature), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
