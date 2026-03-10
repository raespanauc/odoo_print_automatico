"""
OdooPrintMonitor — Entrypoint principal.
Monitorea presupuestos confirmados en Odoo y los imprime automáticamente
en todas las impresoras configuradas.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

from loguru import logger

import config
from odoo_client import OdooClient
from printer import PrinterManager

# ─── Logging ──────────────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logger.remove()
logger.add(sys.stderr, level=config.LOG_LEVEL)
logger.add(
    os.path.join(LOG_DIR, "monitor_{time:YYYY-MM-DD}.log"),
    rotation="00:00",
    retention="30 days",
    level=config.LOG_LEVEL,
)

# ─── Registro local de IDs impresos ──────────────────────────────────────────

# En Docker usa /app/data, en local usa el directorio del proyecto
DATA_DIR = os.path.join(BASE_DIR, "data") if os.path.isdir(os.path.join(BASE_DIR, "data")) else BASE_DIR
PRINTED_FILE = os.path.join(DATA_DIR, "printed_ids.json")


def load_printed_ids() -> dict:
    """Carga registro de IDs impresos: {orders: [...], pickings: [...]}"""
    if os.path.exists(PRINTED_FILE):
        with open(PRINTED_FILE, "r") as f:
            data = json.load(f)
            # Migrar formato antiguo (lista plana)
            if isinstance(data, list):
                return {"orders": [], "pickings": data}
            return data
    return {"orders": [], "pickings": []}


def save_printed_ids(data: dict):
    with open(PRINTED_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ─── Main loop ────────────────────────────────────────────────────────────────


def main():
    logger.info("Iniciando OdooPrintMonitor...")

    # Validar configuración
    if not config.ODOO_USER or not config.ODOO_PASSWORD:
        logger.error("ODOO_USER y ODOO_PASSWORD son obligatorios. Revisa el archivo .env")
        sys.exit(1)

    if not config.PRINTER_NAMES:
        logger.error("PRINTER_NAMES es obligatorio. Revisa el archivo .env")
        sys.exit(1)

    # Inicializar cliente Odoo
    client = OdooClient(
        url=config.ODOO_URL,
        db=config.ODOO_DB,
        user=config.ODOO_USER,
        password=config.ODOO_PASSWORD,
    )

    try:
        client.authenticate()
        client.get_session_cookie()
    except Exception as e:
        logger.error(f"Error en conexión inicial: {e}")
        sys.exit(1)

    # Inicializar impresoras
    try:
        printer = PrinterManager(config.PRINTER_NAMES, config.PRINTER_IPS)
    except RuntimeError as e:
        logger.error(str(e))
        sys.exit(1)

    # Cargar registro de impresos
    printed = load_printed_ids()
    printed_orders = set(printed["orders"])
    printed_pickings = set(printed["pickings"])
    logger.info(
        f"Registro local: {len(printed_orders)} ordenes, "
        f"{len(printed_pickings)} albaranes previamente impresos"
    )

    # Timestamp de inicio
    last_check = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    logger.info(f"Monitoreando desde {last_check} cada {config.POLL_INTERVAL_SECS}s")

    # ─── Loop principal ───────────────────────────────────────────────────────
    while True:
        try:
            orders = client.get_confirmed_orders(last_check)

            for order in orders:
                oid = order["id"]
                oname = order["name"]

                if oid in printed_orders:
                    continue

                logger.info(f"Nuevo pedido confirmado: {oname} (id={oid})")

                # 1. Imprimir PRESUPUESTO 80MM
                try:
                    pdf = client.download_pdf(oid, config.REPORT_PRESUPUESTO)
                    ok = printer.print_pdf(pdf, doc_name=f"Presupuesto {oname}")
                    if not ok:
                        logger.error(f"Fallo impresion presupuesto {oname}")
                        continue
                except Exception as e:
                    logger.error(f"Error PDF presupuesto {oname}: {e}")
                    continue

                # 2. Imprimir albaranes asociados
                picking_ids = order.get("picking_ids", [])
                if picking_ids:
                    pickings = client.get_pickings_by_ids(picking_ids)
                    for pick in pickings:
                        pid = pick["id"]
                        pname = pick["name"]

                        if pid in printed_pickings:
                            continue

                        try:
                            pdf = client.download_pdf(pid, config.REPORT_ALBARAN)
                            ok = printer.print_pdf(pdf, doc_name=f"Albaran {pname}")
                            if ok:
                                printed_pickings.add(pid)
                                logger.info(f"OK albaran {pname} impreso")
                            else:
                                logger.error(f"Fallo impresion albaran {pname}")
                        except Exception as e:
                            logger.error(f"Error PDF albaran {pname}: {e}")

                # Marcar orden como impresa
                printed_orders.add(oid)
                save_printed_ids({
                    "orders": sorted(printed_orders),
                    "pickings": sorted(printed_pickings),
                })
                logger.info(f"OK {oname} procesado completamente")

            # Actualizar timestamp
            last_check = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        except Exception as e:
            logger.warning(f"Error en ciclo de polling: {e}")
            try:
                client.refresh_session()
            except Exception as re:
                logger.error(f"Reconexion fallida: {re}")

        time.sleep(config.POLL_INTERVAL_SECS)


if __name__ == "__main__":
    main()
