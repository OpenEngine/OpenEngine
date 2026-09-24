"""OAuth state and persistent ES256 keys; schema belongs to Alembic."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import sqlite3

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from filelock import FileLock
import jwt


class OAuthStore:
    def __init__(self, path: str | Path):
        self.path = str(path)

    @contextmanager
    def transaction(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def revoke_all(self):
        with self.transaction() as db:
            db.execute("UPDATE oauth_refresh_tokens SET revoked = 1")
            # Outstanding codes must not mint a new family after revocation.
            db.execute("DELETE FROM oauth_codes")


class SigningKeys:
    """Reload a protected key ring on use, allowing rotation without restart.

    Old public keys remain published; only the newest private key is retained.
    Atomic replacement and an interprocess lock protect initialization/rotation.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        with FileLock(str(self.path) + ".lock"):
            if not self.path.exists():
                self._write([])
        self._read()

    def _read(self):
        if self.path.stat().st_mode & 0o077:
            raise ValueError("OAuth signing key file must have mode 600")
        return json.loads(self.path.read_text())

    def _write(self, previous):
        key = ec.generate_private_key(ec.SECP256R1())
        kid = secrets.token_urlsafe(16)
        public = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
        public.update(kid=kid, use="sig", alg="ES256")
        data = {"kid": kid, "private": key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(), "keys": [*previous, public]}
        temporary = self.path.with_name(self.path.name + "." + secrets.token_hex(8))
        try:
            with os.fdopen(os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as stream:
                json.dump(data, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def rotate(self):
        with FileLock(str(self.path) + ".lock"):
            self._write(self._read()["keys"])

    def jwks(self):
        return {"keys": self._read()["keys"]}

    def sign(self, claims):
        ring = self._read()
        return jwt.encode(claims, ring["private"], algorithm="ES256",
                          headers={"kid": ring["kid"], "typ": "at+jwt"})


def main():
    parser = argparse.ArgumentParser(description="Administer the web MCP OAuth issuer")
    parser.add_argument("action", choices=["revoke-all", "rotate-key"])
    parser.add_argument("database", type=Path, help="OE state SQLite file")
    args = parser.parse_args()
    if not args.database.is_file():
        parser.error("state database does not exist")
    if args.action == "revoke-all":
        OAuthStore(args.database).revoke_all()
    else:
        SigningKeys(str(args.database) + ".oauth-keys.json").rotate()


if __name__ == "__main__":
    main()
