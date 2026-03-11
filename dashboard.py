"""
Dashboard web para OdooPrintMonitor.
Se ejecuta en un hilo de fondo desde monitor.py.
"""

import threading

from flask import Flask, render_template, jsonify, request
from loguru import logger

import config
from print_store import PrintStore

app = Flask(__name__)
store = PrintStore()

# Estado compartido (actualizado por monitor.py)
_monitor_state = {
    "running": False,
    "last_poll": None,
    "last_order": None,
}
_state_lock = threading.Lock()


def update_state(**kwargs):
    with _state_lock:
        _monitor_state.update(kwargs)


@app.route("/")
def index():
    printers = store.get_printers_with_status()

    with _state_lock:
        state = dict(_monitor_state)

    return render_template(
        "dashboard.html",
        printers=printers,
        stats=store.get_stats(),
        recent=store.get_recent_jobs(50),
        state=state,
    )


@app.route("/api/status")
def api_status():
    printers = store.get_printers_with_status()

    with _state_lock:
        state = dict(_monitor_state)

    return jsonify({
        "printers": printers,
        "stats": store.get_stats(),
        "recent": store.get_recent_jobs(20),
        "state": state,
    })


# ─── CRUD Impresoras ─────────────────────────────────────────────────────────


@app.route("/api/printers", methods=["GET"])
def api_printers_list():
    return jsonify(store.get_printers_with_status())


@app.route("/api/printers", methods=["POST"])
def api_printers_add():
    data = request.json
    name = data.get("name", "").strip()
    ip = data.get("ip", "").strip()
    port = int(data.get("port", 9100))
    if not name or not ip:
        return jsonify({"error": "name e ip son obligatorios"}), 400
    store.add_printer(name, ip, port)
    logger.info(f"Impresora agregada: {name} ({ip}:{port})")
    return jsonify({"ok": True})


@app.route("/api/printers/<int:pid>", methods=["PUT"])
def api_printers_update(pid):
    data = request.json
    store.update_printer(
        pid,
        name=data.get("name"),
        ip=data.get("ip"),
        port=data.get("port"),
        enabled=data.get("enabled"),
    )
    logger.info(f"Impresora {pid} actualizada")
    return jsonify({"ok": True})


@app.route("/api/printers/<int:pid>", methods=["DELETE"])
def api_printers_delete(pid):
    store.delete_printer(pid)
    logger.info(f"Impresora {pid} eliminada")
    return jsonify({"ok": True})


def start_dashboard(port=None):
    """Inicia Flask en un hilo de fondo."""
    port = port or config.DASHBOARD_PORT
    t = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, debug=False),
        daemon=True,
    )
    t.start()
    logger.info(f"Dashboard web en http://0.0.0.0:{port}")
