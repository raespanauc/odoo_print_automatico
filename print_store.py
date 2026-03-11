"""
PrintStore — Registro persistente de impresiones usando SQLite.
Thread-safe para uso concurrente entre monitor y dashboard.
"""

import os
import sqlite3
import threading
from datetime import datetime, timezone


class PrintStore:
    def __init__(self, db_path=None):
        base = os.path.dirname(os.path.abspath(__file__))
        data_dir = os.path.join(base, "data")
        os.makedirs(data_dir, exist_ok=True)
        self.db_path = db_path or os.path.join(data_dir, "print_history.db")
        self._local = threading.local()
        self._init_db()

    @property
    def _conn(self):
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path)
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def _init_db(self):
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS print_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                doc_type TEXT NOT NULL,
                doc_name TEXT NOT NULL,
                record_id INTEGER NOT NULL,
                printer TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ok',
                error_msg TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_record_type
                ON print_jobs(record_id, doc_type);
            CREATE INDEX IF NOT EXISTS idx_timestamp
                ON print_jobs(timestamp DESC);
        """)
        self._conn.commit()

    # ─── Escritura ───────────────────────────────────────────────────────────

    def record_print(self, doc_type, doc_name, record_id, printer,
                     status="ok", error_msg=None):
        self._conn.execute(
            "INSERT INTO print_jobs "
            "(timestamp, doc_type, doc_name, record_id, printer, status, error_msg) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
             doc_type, doc_name, record_id, printer, status, error_msg),
        )
        self._conn.commit()

    # ─── Consultas ───────────────────────────────────────────────────────────

    def is_printed(self, record_id, doc_type):
        row = self._conn.execute(
            "SELECT 1 FROM print_jobs "
            "WHERE record_id=? AND doc_type=? AND status='ok' LIMIT 1",
            (record_id, doc_type),
        ).fetchone()
        return row is not None

    def get_printed_order_ids(self):
        rows = self._conn.execute(
            "SELECT DISTINCT record_id FROM print_jobs "
            "WHERE doc_type='presupuesto' AND status='ok'"
        ).fetchall()
        return {r[0] for r in rows}

    def get_printed_picking_ids(self):
        rows = self._conn.execute(
            "SELECT DISTINCT record_id FROM print_jobs "
            "WHERE doc_type='albaran' AND status='ok'"
        ).fetchall()
        return {r[0] for r in rows}

    def get_recent_jobs(self, limit=50):
        rows = self._conn.execute(
            "SELECT * FROM print_jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_stats(self):
        c = self._conn
        total = c.execute("SELECT COUNT(*) FROM print_jobs").fetchone()[0]
        ok = c.execute(
            "SELECT COUNT(*) FROM print_jobs WHERE status='ok'"
        ).fetchone()[0]
        errors = c.execute(
            "SELECT COUNT(*) FROM print_jobs WHERE status='error'"
        ).fetchone()[0]
        orders = c.execute(
            "SELECT COUNT(DISTINCT record_id) FROM print_jobs "
            "WHERE doc_type='presupuesto' AND status='ok'"
        ).fetchone()[0]
        pickings = c.execute(
            "SELECT COUNT(DISTINCT record_id) FROM print_jobs "
            "WHERE doc_type='albaran' AND status='ok'"
        ).fetchone()[0]
        return {
            "total_jobs": total,
            "ok": ok,
            "errors": errors,
            "orders_printed": orders,
            "pickings_printed": pickings,
        }

    # ─── Migración desde printed_ids.json ────────────────────────────────────

    def import_from_json(self, json_path):
        """Importa IDs del antiguo printed_ids.json."""
        import json
        if not os.path.exists(json_path):
            return 0
        with open(json_path) as f:
            data = json.load(f)
        if isinstance(data, list):
            data = {"orders": [], "pickings": data}

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        count = 0
        for oid in data.get("orders", []):
            if not self.is_printed(oid, "presupuesto"):
                self._conn.execute(
                    "INSERT INTO print_jobs "
                    "(timestamp, doc_type, doc_name, record_id, printer, status) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (ts, "presupuesto", f"(migrado)", oid, "migrado", "ok"),
                )
                count += 1
        for pid in data.get("pickings", []):
            if not self.is_printed(pid, "albaran"):
                self._conn.execute(
                    "INSERT INTO print_jobs "
                    "(timestamp, doc_type, doc_name, record_id, printer, status) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (ts, "albaran", f"(migrado)", pid, "migrado", "ok"),
                )
                count += 1
        self._conn.commit()
        return count
