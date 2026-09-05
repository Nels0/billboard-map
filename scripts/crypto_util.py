"""AES-256-GCM envelope shared by the build script and the browser.

The published page is world-readable, so the map data is only ever committed as
ciphertext. The envelope is deliberately plain: PBKDF2-SHA256 to stretch the
passphrase, then AES-GCM, both of which WebCrypto implements natively so the
page needs no third-party crypto library.
"""

import base64
import json
import os
import sys
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITERATIONS = 600_000
TIERS_VERSION = 2  # the multi-tier container written by write_tiers()
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


def write_tiers(path: Path, tiers: dict[str, tuple[object, str]]) -> None:
    """Write several independently-encrypted payloads into one file.

    Shape is {"v": 2, "tiers": {name: envelope}}, where each envelope is exactly
    what encrypt_json() returns. One file rather than one per tier so the "no
    stray files under site/" guard keeps its allowlist; the page tries each
    tier's envelope in turn and keeps whichever one the passphrase opens.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "v": TIERS_VERSION,
        "tiers": {
            name: encrypt_json(payload, passphrase)
            for name, (payload, passphrase) in tiers.items()
        },
    }
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")


def read_tier(document: dict, tier: str, passphrase: str) -> object:
    """Decrypt one tier of a write_tiers() file, or a bare v1 envelope."""
    envelope = document.get("tiers", {}).get(tier) if "tiers" in document else document
    if envelope is None:
        sys.exit(f"no {tier!r} tier in this file; have {sorted(document.get('tiers', {}))}")
    return decrypt_json(envelope, passphrase)


def passphrase_env(tier: str) -> str:
    """Which environment variable holds a given tier's passphrase.

    "full" keeps the original MAP_PASSPHRASE so nothing about the existing
    deployment has to change; every other tier gets MAP_PASSPHRASE_<TIER>.
    """
    return "MAP_PASSPHRASE" if tier == "full" else f"MAP_PASSPHRASE_{tier.upper()}"


def _cli() -> None:
    """Encrypt/decrypt a JSON file in place-ish, for the geocode cache in CI.

    The cache maps addresses to coordinates, so it is committed encrypted even
    though the repository has to be public for GitHub Pages.
    """
    if len(sys.argv) not in (4, 5) or sys.argv[1] not in {"encrypt", "decrypt"}:
        sys.exit("usage: crypto_util.py {encrypt|decrypt} <src> <dst> [tier]")

    mode, src, dst = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    # A tier only makes sense for reading site/data.enc.json back; the cache is
    # a single envelope under MAP_PASSPHRASE and always has been.
    tier = sys.argv[4] if len(sys.argv) == 5 else None
    var = passphrase_env(tier) if tier else "MAP_PASSPHRASE"
    passphrase = os.environ.get(var)
    if not passphrase:
        sys.exit(f"{var} is not set.")

    if not src.exists():
        # First ever run: nothing cached yet, which is not an error.
        print(f"{src} absent; skipping {mode}")
        return

    payload = json.loads(src.read_text(encoding="utf-8"))
    if mode == "encrypt":
        result = encrypt_json(payload, passphrase)
    elif tier:
        result = read_tier(payload, tier, passphrase)
    else:
        result = decrypt_json(payload, passphrase)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{mode}ed {src.name}{f' [{tier}]' if tier else ''} -> {dst}")


if __name__ == "__main__":
    _cli()
