import os
from dotenv import load_dotenv

load_dotenv()

ODOO_URL = os.getenv("ODOO_URL", "https://repuestosespana.odoo.com")
ODOO_DB = os.getenv("ODOO_DB", "repuestosespana")
ODOO_USER = os.getenv("ODOO_USER")
ODOO_APIKEY = os.getenv("ODOO_APIKEY")
PRINTER_NAME = os.getenv("PRINTER_NAME", "EPSON TM-T20II TOTEM")
POLL_INTERVAL_SECS = int(os.getenv("POLL_INTERVAL_SECS", "8"))
REPORT_ACTION = os.getenv("REPORT_ACTION", "stock.report_deliveryslip80")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
