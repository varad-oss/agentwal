"""Mock payments service: a separate local HTTP process backed by SQLite.

    POST /payments            body {"run_id", "ticket_id", "amount_cents"}
                              header Idempotency-Key (optional)
    GET  /payments?key=K      reconciliation lookup: the payment made with key K, or 404
    GET  /payments?run_id=R   every payment for a run (used by the invariant checker)
    GET  /health

With idempotency on (the default) a repeated key returns the original payment instead of
charging again, and a repeated key with a different body is rejected with 409, like
Stripe. `--no-idempotency` turns deduplication off (keys are still stored, so the
reconciliation lookup keeps working) to show what breaks.

Run: python -m demo.payments_service --db results/payments.db --port 0
It prints "READY <port>" once it is listening.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SCHEMA = """
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT,
    run_id TEXT NOT NULL,
    ticket_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    created_at REAL DEFAULT (julianday('now'))
);
CREATE INDEX IF NOT EXISTS payments_key ON payments(idempotency_key);
CREATE INDEX IF NOT EXISTS payments_run ON payments(run_id);
"""


def row_to_payment(row) -> dict:
    return {"payment_id": f"pay_{row[0]}", "idempotency_key": row[1], "run_id": row[2],
            "ticket_id": row[3], "amount_cents": row[4]}


class PaymentsDB:
    def __init__(self, path: str, idempotent: bool = True):
        self.idempotent = idempotent
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()

    def charge(self, key: str | None, run_id: str, ticket_id: str, amount_cents: int) -> tuple[int, dict]:
        cols = "id, idempotency_key, run_id, ticket_id, amount_cents"
        with self.lock:
            if self.idempotent and key:
                row = self.conn.execute(f"SELECT {cols} FROM payments WHERE idempotency_key=? ORDER BY id LIMIT 1", (key,)).fetchone()
                if row:
                    p = row_to_payment(row)
                    if (p["run_id"], p["ticket_id"], p["amount_cents"]) != (run_id, ticket_id, amount_cents):
                        return 409, {"error": "idempotency key reused with different parameters", "original": p}
                    return 200, {**p, "replayed": True}
            cur = self.conn.execute(
                "INSERT INTO payments (idempotency_key, run_id, ticket_id, amount_cents) VALUES (?,?,?,?)",
                (key, run_id, ticket_id, amount_cents),
            )
            row = self.conn.execute(f"SELECT {cols} FROM payments WHERE id=?", (cur.lastrowid,)).fetchone()
            return 201, {**row_to_payment(row), "replayed": False}

    def by_key(self, key: str) -> dict | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT id, idempotency_key, run_id, ticket_id, amount_cents FROM payments WHERE idempotency_key=? ORDER BY id LIMIT 1",
                (key,),
            ).fetchone()
        return row_to_payment(row) if row else None

    def by_run(self, run_id: str) -> list[dict]:
        with self.lock:
            rows = self.conn.execute(
                "SELECT id, idempotency_key, run_id, ticket_id, amount_cents FROM payments WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall()
        return [row_to_payment(r) for r in rows]


def make_handler(db: PaymentsDB):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):  # keep the chaos output quiet
            pass

        def _send(self, code: int, body: dict | list) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                # The agent was killed after sending the request: the payment is already
                # committed but the response is lost. That is crash window ③, not an error.
                pass

        def do_GET(self):
            url = urlparse(self.path)
            q = parse_qs(url.query)
            if url.path == "/health":
                return self._send(200, {"ok": True, "idempotent": db.idempotent})
            if url.path == "/payments" and "key" in q:
                p = db.by_key(q["key"][0])
                return self._send(200, p) if p else self._send(404, {"error": "not found"})
            if url.path == "/payments" and "run_id" in q:
                return self._send(200, db.by_run(q["run_id"][0]))
            self._send(404, {"error": "no route"})

        def do_POST(self):
            if urlparse(self.path).path != "/payments":
                return self._send(404, {"error": "no route"})
            n = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(n))
                code, resp = db.charge(
                    self.headers.get("Idempotency-Key"), body["run_id"], body["ticket_id"], int(body["amount_cents"])
                )
            except (ValueError, KeyError) as e:
                code, resp = 400, {"error": str(e)}
            self._send(code, resp)

    return Handler


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-idempotency", action="store_true")
    args = ap.parse_args(argv)
    db = PaymentsDB(args.db, idempotent=not args.no_idempotency)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(db))
    server.daemon_threads = True
    print(f"READY {server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        sys.stdout.flush()


if __name__ == "__main__":
    main()
