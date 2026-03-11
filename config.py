import os
import sys
from dotenv import load_dotenv

# Permite elegir archivo .env: python monitor.py --env .env.desarrollo
env_file = ".env"
if "--env" in sys.argv:
    idx = sys.argv.index("--env")
    if idx + 1 < len(sys.argv):
        env_file = sys.argv[idx + 1]

load_dotenv(env_file)

# Odoo
ODOO_URL = os.getenv("ODOO_URL", "https://repuestosespana.odoo.com")
ODOO_DB = os.getenv("ODOO_DB", "repuestosespana")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")

# Polling
POLL_INTERVAL_SECS = int(os.getenv("POLL_INTERVAL_SECS", "8"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Impresoras (separadas por coma)
PRINTER_NAMES = [
    p.strip() for p in os.getenv("PRINTER_NAMES", "").split(",") if p.strip()
]

# IPs de impresoras (legacy, para migración inicial a SQLite)
# Formato: nombre=ip,nombre2=ip2 o nombre=ip;nombre2=ip2
_raw_ips = os.getenv("PRINTER_IPS", "")
_separator = ";" if ";" in _raw_ips else ","
PRINTER_IPS = {}
for entry in _raw_ips.split(_separator):
    entry = entry.strip()
    if "=" in entry:
        name, ip = entry.split("=", 1)
        PRINTER_IPS[name.strip()] = ip.strip()

# Reporte de presupuesto 80mm para sale.order
REPORT_PRESUPUESTO = os.getenv(
    "REPORT_PRESUPUESTO",
    "studio_customization.studio_report_docume_b45c5d74-d0f6-45ee-9dd7-b5020d9fc920",
)

# Reporte de albarán para stock.picking
REPORT_ALBARAN = os.getenv("REPORT_ALBARAN", "stock.report_deliveryslip")

# Dashboard web
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "5000"))
