"""
Dashboard web para OdooPrintMonitor.
Se ejecuta en un hilo de fondo desde monitor.py.
"""

import socket
import threading

from flask import Flask, render_template, jsonify
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


def _check_printer(ip, port=9100, timeout=3):
    try:
        s = socket.create_connection((ip, port), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


@app.route("/")
def index():
    printers = []
    for name in config.PRINTER_NAMES:
        ip = config.PRINTER_IPS.get(name, "")
        online = _check_printer(ip) if ip else False
        printers.append({"name": name, "ip": ip, "online": online})

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
    printers = []
    for name in config.PRINTER_NAMES:
        ip = config.PRINTER_IPS.get(name, "")
        online = _check_printer(ip) if ip else False
        printers.append({"name": name, "ip": ip, "online": online})

    with _state_lock:
        state = dict(_monitor_state)

    return jsonify({
        "printers": printers,
        "stats": store.get_stats(),
        "recent": store.get_recent_jobs(20),
        "state": state,
    })


def start_dashboard(port=None):
    """Inicia Flask en un hilo de fondo."""
    port = port or config.DASHBOARD_PORT
    t = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, debug=False),
        daemon=True,
    )
    t.start()
    logger.info(f"Dashboard web en http://0.0.0.0:{port}")
