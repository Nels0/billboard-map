"""AES-256-GCM envelope shared by the build script and the browser.

The published page is world-readable, so the map data is only ever committed as
ciphertext. The envelope is deliberately plain: PBKDF2-SHA256 to stretch the
passphrase, then AES-GCM, both of which WebCrypto implements natively so the
page needs no third-party crypto library.
"""

import base64
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITERATIONS = 600_000
SALT_BYTES = 16
IV_BYTES = 12


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def derive_key(passphrase: str, salt: bytes, iterations: int = ITERATIONS) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterations
    )
    return kdf.derive(passphrase.encode("utf-8"))


def encrypt_json(payload: object, passphrase: str) -> dict:
    salt = os.urandom(SALT_BYTES)
    iv = os.urandom(IV_BYTES)
    plaintext = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    # AESGCM appends the 16-byte tag, which is exactly what WebCrypto expects.
    ciphertext = AESGCM(derive_key(passphrase, salt)).encrypt(iv, plaintext, None)
    return {
        "v": 1,
        "kdf": "PBKDF2-SHA256",
        "iterations": ITERATIONS,
        "salt": _b64(salt),
        "iv": _b64(iv),
        "ct": _b64(ciphertext),
    }


def decrypt_json(envelope: dict, passphrase: str) -> object:
    salt = base64.b64decode(envelope["salt"])
    iv = base64.b64decode(envelope["iv"])
    ciphertext = base64.b64decode(envelope["ct"])
    key = derive_key(passphrase, salt, envelope.get("iterations", ITERATIONS))
    return json.loads(AESGCM(key).decrypt(iv, ciphertext, None).decode("utf-8"))


def write_encrypted(path: Path, payload: object, passphrase: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    envelope = encrypt_json(payload, passphrase)
    path.write_text(json.dumps(envelope, indent=2), encoding="utf-8")


def _cli() -> None:
    """Encrypt/decrypt a JSON file in place-ish, for the geocode cache in CI.

    The cache maps addresses to coordinates, so it is committed encrypted even
    though the repository has to be public for GitHub Pages.
    """
    import sys

    if len(sys.argv) != 4 or sys.argv[1] not in {"encrypt", "decrypt"}:
        sys.exit("usage: crypto_util.py {encrypt|decrypt} <src> <dst>")

    mode, src, dst = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    passphrase = os.environ.get("MAP_PASSPHRASE")
    if not passphrase:
        sys.exit("MAP_PASSPHRASE is not set.")

    if not src.exists():
        # First ever run: nothing cached yet, which is not an error.
        print(f"{src} absent; skipping {mode}")
        return

    payload = json.loads(src.read_text(encoding="utf-8"))
    result = (
        encrypt_json(payload, passphrase)
        if mode == "encrypt"
        else decrypt_json(payload, passphrase)
    )
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{mode}ed {src.name} -> {dst}")


if __name__ == "__main__":
    _cli()
