"""Host-owned identities and durable VM metadata, never guest-controlled paths."""

import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .errors import ServiceError
from .images import IMAGE_ID


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.directory.stat().st_mode & 0o077:
            raise ValueError("state directory must have mode 0700")
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.directory / "state.sqlite", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS workspaces (
                id TEXT PRIMARY KEY, max_vms INTEGER NOT NULL,
                max_memory_mib INTEGER NOT NULL, max_vcpus INTEGER NOT NULL,
                network INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS tokens (
                digest TEXT PRIMARY KEY, workspace TEXT NOT NULL,
                expires REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS vms (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, slot INTEGER NOT NULL,
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS receipts (
                owner TEXT NOT NULL, operation TEXT NOT NULL, key TEXT NOT NULL,
                digest TEXT NOT NULL, result TEXT,
                PRIMARY KEY(owner, operation, key)
            );
        """)
        columns = {r["name"] for r in self.db.execute("PRAGMA table_info(workspaces)")}
        if "images" not in columns:
            # NULL means any configured image, preserving pre-existing workspaces.
            with self.db:
                self.db.execute("ALTER TABLE workspaces ADD COLUMN images TEXT")
        os.chmod(self.directory / "state.sqlite", 0o600)

    @contextmanager
    def transaction(self):
        with self.lock, self.db:
            yield self.db

    def issue(
        self,
        workspace,
        *,
        ttl=3600,
        max_vms=1,
        max_memory_mib=2048,
        max_vcpus=2,
        network=False,
        images=None,
    ):
        if not workspace or len(workspace) > 128 or ttl <= 0:
            raise ValueError("invalid workspace or token lifetime")
        if not 1 <= max_vms <= 64 or not 256 <= max_memory_mib <= 8192:
            raise ValueError("invalid quota")
        if not 1 <= max_vcpus <= 8:
            raise ValueError("invalid CPU quota")
        if images is not None:
            if not images or not all(re.fullmatch(IMAGE_ID, i) for i in images):
                raise ValueError("invalid image restriction")
            images = json.dumps(sorted(set(images)))
        token = secrets.token_urlsafe(32)
        with self.transaction() as db:
            existing = db.execute("SELECT * FROM workspaces WHERE id=?", (workspace,)).fetchone()
            if existing and existing["revoked"]:
                raise ValueError("workspace is revoked; provision a new workspace")
            # Rotation preserves policy and ownership. It cannot silently widen permissions.
            db.execute(
                """INSERT OR IGNORE INTO workspaces
                (id, max_vms, max_memory_mib, max_vcpus, network, revoked, images)
                VALUES (?, ?, ?, ?, ?, 0, ?)""",
                (workspace, max_vms, max_memory_mib, max_vcpus, int(network), images),
            )
            db.execute(
                "INSERT INTO tokens VALUES (?, ?, ?, 0)",
                (hashlib.sha256(token.encode()).hexdigest(), workspace, time.time() + ttl),
            )
        return token

    def authenticate(self, token):
        if not token or len(token) > 256:
            raise ServiceError("unauthorized", "Valid workspace credential required", 401)
        with self.transaction() as db:
            row = db.execute(
                """SELECT w.*, t.expires, t.revoked AS token_revoked
                FROM tokens t JOIN workspaces w ON w.id=t.workspace WHERE t.digest=?""",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
        if not row or row["revoked"] or row["token_revoked"] or row["expires"] <= time.time():
            raise ServiceError("unauthorized", "Valid workspace credential required", 401)
        grant = dict(row)
        grant["images"] = None if grant["images"] is None else json.loads(grant["images"])
        return grant

    def revoke(self, workspace):
        with self.transaction() as db:
            db.execute("UPDATE workspaces SET revoked=1 WHERE id=?", (workspace,))

    def save_vm(self, vm):
        with self.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO vms VALUES (?, ?, ?, ?)",
                (vm["id"], vm["owner"], vm["slot"], json.dumps(vm)),
            )

    def vms(self):
        with self.transaction() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT data FROM vms")]

    def reserve(self, owner, operation, key, args):
        digest = hashlib.sha256(json.dumps(args, sort_keys=True).encode()).hexdigest()
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM receipts WHERE owner=? AND operation=? AND key=?",
                (owner, operation, key),
            ).fetchone()
            if row:
                if row["digest"] != digest:
                    raise ServiceError(
                        "idempotency_conflict", "Request ID reused with new arguments", 409
                    )
                if row["result"] is None:
                    raise ServiceError(
                        "outcome_unknown", "Previous request needs reconciliation", 409
                    )
                return json.loads(row["result"])
            count = db.execute("SELECT COUNT(*) FROM receipts WHERE owner=?", (owner,)).fetchone()[
                0
            ]
            if count >= 4096:
                raise ServiceError("quota_exceeded", "Workspace request history limit reached", 409)
            db.execute(
                "INSERT INTO receipts VALUES (?, ?, ?, ?, NULL)", (owner, operation, key, digest)
            )
        return None

    def finish(self, owner, operation, key, result):
        with self.transaction() as db:
            db.execute(
                "UPDATE receipts SET result=? WHERE owner=? AND operation=? AND key=?",
                (json.dumps(result), owner, operation, key),
            )
