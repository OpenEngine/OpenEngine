"""OAuth state and persistent ES256 keys; schema belongs to Alembic."""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

from starlette.concurrency import run_in_threadpool

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from filelock import FileLock
import jwt


ACCESS_TOKEN_TTL = 900
CLIENT_TTL = 30 * 86400
MAX_CLIENTS = 1000


class OAuthStore:
    def __init__(self, path: str | Path):
        self.path = str(path)

    @contextmanager
    def transaction(self, *, write=True):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    async def run(self, operation, *, write=True):
        """Keep connection creation, transaction, and close on one worker."""
        def execute():
            with self.transaction(write=write) as db:
                return operation(db)
        return await run_in_threadpool(execute)

    def register(self, client_id, document):
        with self.transaction() as db:
            now = int(time.time())
            db.execute("DELETE FROM oauth_clients WHERE expires <= ?", (now,))
            if db.execute("SELECT count(*) FROM oauth_clients").fetchone()[0] >= MAX_CLIENTS:
                return False
            db.execute("INSERT INTO oauth_clients (client_id, metadata, expires) VALUES (?, ?, ?)",
                       (client_id, json.dumps(document), now + CLIENT_TTL))
            return True

    def revoke_all(self):
        with self.transaction() as db:
            db.execute("UPDATE oauth_refresh_tokens SET revoked = 1")
            # Outstanding codes must not mint a new family after revocation.
            db.execute("DELETE FROM oauth_codes")


class SigningKeys:
    """Reload a protected key ring on use, allowing rotation without restart.

    Old public keys overlap for one access-token lifetime; only the newest
    private key is retained.
    Atomic replacement and an interprocess lock protect initialization/rotation.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        with FileLock(str(self.path) + ".lock"):
            if not self.path.exists():
                self._write([])
            # Give legacy public keys a fixed retirement deadline on upgrade.
            ring = self._read()
            if "retire_at" not in ring:
                ring["retire_at"] = {key["kid"]: time.time() + ACCESS_TOKEN_TTL
                                     for key in ring["keys"] if key["kid"] != ring["kid"]}
                self._save(ring)

    def _read(self):
        if self.path.stat().st_mode & 0o077:
            raise ValueError("OAuth signing key file must have mode 600")
        return json.loads(self.path.read_text())

    def _write(self, previous, retire_at=None):
        key = ec.generate_private_key(ec.SECP256R1())
        kid = secrets.token_urlsafe(16)
        public = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
        public.update(kid=kid, use="sig", alg="ES256")
        data = {"kid": kid, "private": key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode(), "keys": [*previous, public], "retire_at": retire_at or {}}
        self._save(data)

    def _save(self, data):
        temporary = self.path.with_name(self.path.name + "." + secrets.token_hex(8))
        try:
            with os.fdopen(os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as stream:
                json.dump(data, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def _live_keys(self, ring):
        return [key for key in ring["keys"]
                if key["kid"] == ring["kid"] or ring["retire_at"].get(key["kid"], 0) > time.time()]

    def rotate(self):
        with FileLock(str(self.path) + ".lock"):
            ring = self._read()
            previous = self._live_keys(ring)
            deadlines = {key["kid"]: ring["retire_at"].get(key["kid"], time.time() + ACCESS_TOKEN_TTL)
                         for key in previous}
            self._write(previous, deadlines)

    def retire(self, kid):
        """Immediately remove a compromised key, replacing it if active."""
        with FileLock(str(self.path) + ".lock"):
            ring = self._read()
            if not any(key["kid"] == kid for key in ring["keys"]):
                raise ValueError("unknown signing key ID")
            ring["keys"] = [key for key in self._live_keys(ring) if key["kid"] != kid]
            ring["retire_at"] = {key["kid"]: ring["retire_at"][key["kid"]]
                                 for key in ring["keys"] if key["kid"] != ring["kid"]}
            if kid == ring["kid"]:
                self._write(ring["keys"], ring["retire_at"])
            else:
                self._save(ring)

    def jwks(self):
        return {"keys": self._live_keys(self._read())}

    def sign(self, claims):
        # Serialize signing with rotation so the overlap starts after the last signature.
        with FileLock(str(self.path) + ".lock"):
            ring = self._read()
            return jwt.encode(claims, ring["private"], algorithm="ES256",
                              headers={"kid": ring["kid"], "typ": "at+jwt"})


def main():
    parser = argparse.ArgumentParser(description="Administer the web MCP OAuth issuer")
    parser.add_argument("action", choices=["revoke-all", "rotate-key", "retire-key"])
    parser.add_argument("database", type=Path, help="OE state SQLite file")
    parser.add_argument("--kid", help="signing key ID to retire immediately")
    args = parser.parse_args()
    if args.action == "retire-key" and not args.kid:
        parser.error("retire-key requires --kid")
    if not args.database.is_file():
        parser.error("state database does not exist")
    if args.action == "revoke-all":
        OAuthStore(args.database).revoke_all()
    else:
        keys = SigningKeys(str(args.database) + ".oauth-keys.json")
        if args.action == "retire-key":
            keys.retire(args.kid)
        else:
            keys.rotate()


if __name__ == "__main__":
    main()
