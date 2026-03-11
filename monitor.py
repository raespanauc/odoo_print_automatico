"""
OdooPrintMonitor — Entrypoint principal.
Monitorea presupuestos confirmados en Odoo y los imprime automáticamente
en todas las impresoras configuradas.
"""

import os
import re
import sys
import time
from datetime import datetime, timezone

from loguru import logger

import config
from odoo_client import OdooClient
from printer import PrinterManager
from print_store import PrintStore
from dashboard import start_dashboard, update_state

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

# ─── Migración legacy ────────────────────────────────────────────────────────

LEGACY_FILE = os.path.join(BASE_DIR, "printed_ids.json")
DATA_DIR = os.path.join(BASE_DIR, "data")
LEGACY_FILE_ALT = os.path.join(DATA_DIR, "printed_ids.json")


# ─── Utilidades ──────────────────────────────────────────────────────────────

# Extrae zona del nombre del albarán: "01/OUT/377998 - Z1 - 01 de 02" → "Z1"
_ZONE_RE = re.compile(r"- (Z\d+) -", re.IGNORECASE)


def extract_zone(picking_name: str) -> str | None:
    """Extrae la zona (Z1, Z2, etc.) del nombre del albarán."""
    m = _ZONE_RE.search(picking_name)
    return m.group(1).upper() if m else None


# ─── Main loop ────────────────────────────────────────────────────────────────


def main():
    logger.info("Iniciando OdooPrintMonitor...")

    # Validar configuración
    if not config.ODOO_USER or not config.ODOO_PASSWORD:
        logger.error("ODOO_USER y ODOO_PASSWORD son obligatorios. Revisa el archivo .env")
        sys.exit(1)

    # Inicializar store
    store = PrintStore()

    # Migrar datos legacy (printed_ids.json)
    for legacy in (LEGACY_FILE, LEGACY_FILE_ALT):
        if os.path.exists(legacy):
            count = store.import_from_json(legacy)
            if count:
                logger.info(f"Migrados {count} registros desde {legacy}")
            os.rename(legacy, legacy + ".bak")

    # Migrar impresoras desde env vars (solo la primera vez)
    if config.PRINTER_NAMES and config.PRINTER_IPS:
        count = store.import_from_env(config.PRINTER_NAMES, config.PRINTER_IPS)
        if count:
            logger.info(f"Migradas {count} impresoras desde variables de entorno")

    # Verificar que hay impresoras
    printers_db = store.get_printers(only_enabled=True)
    if not printers_db:
        logger.warning(
            "No hay impresoras configuradas. "
            "Agregá impresoras desde el dashboard web."
        )

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

    # Inicializar impresoras desde DB
    printer = PrinterManager(store)

    # Cargar IDs ya impresos
    printed_orders = store.get_printed_order_ids()
    printed_pickings = store.get_printed_picking_ids()
    logger.info(
        f"Registro: {len(printed_orders)} ordenes, "
        f"{len(printed_pickings)} albaranes previamente impresos"
    )

    # Iniciar dashboard web
    start_dashboard()
    update_state(running=True)

    # Timestamp de inicio
    last_check = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    logger.info(f"Monitoreando desde {last_check} cada {config.POLL_INTERVAL_SECS}s")

    # ─── Loop principal ───────────────────────────────────────────────────────
    while True:
        try:
            orders = client.get_confirmed_orders(last_check)
            update_state(last_poll=datetime.now(timezone.utc).isoformat())

            for order in orders:
                oid = order["id"]
                oname = order["name"]

                if oid in printed_orders:
                    continue

                logger.info(f"Nuevo pedido confirmado: {oname} (id={oid})")
                update_state(last_order=oname)

                # 1. Imprimir PRESUPUESTO 80MM
                try:
                    pdf = client.download_pdf(oid, config.REPORT_PRESUPUESTO)
                    results = printer.print_pdf(pdf, doc_name=f"Presupuesto {oname}")
                    any_ok = False
                    for r in results:
                        store.record_print(
                            "presupuesto", oname, oid, r["printer"],
                            r["status"], r.get("error"),
                        )
                        if r["status"] == "ok":
                            any_ok = True
                    if not any_ok:
                        logger.error(f"Fallo impresion presupuesto {oname} en todas las impresoras")
                        continue
                except Exception as e:
                    logger.error(f"Error PDF presupuesto {oname}: {e}")
                    store.record_print(
                        "presupuesto", oname, oid, "N/A", "error", str(e),
                    )
                    continue

                # Pausa para que la impresora procese el presupuesto
                time.sleep(5)

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
                            zone = extract_zone(pname)
                            if zone:
                                logger.info(f"Albaran {pname} → zona {zone}")
                            pdf = client.download_pdf(pid, config.REPORT_ALBARAN)
                            results = printer.print_pdf(
                                pdf, doc_name=f"Albaran {pname}", zone=zone,
                            )
                            any_ok = False
                            for r in results:
                                store.record_print(
                                    "albaran", pname, pid, r["printer"],
                                    r["status"], r.get("error"),
                                )
                                if r["status"] == "ok":
                                    any_ok = True
                            if any_ok:
                                printed_pickings.add(pid)
                                logger.info(f"OK albaran {pname} impreso")
                                time.sleep(3)
                            else:
                                logger.error(f"Fallo impresion albaran {pname}")
                        except Exception as e:
                            logger.error(f"Error PDF albaran {pname}: {e}")
                            store.record_print(
                                "albaran", pname, pid, "N/A", "error", str(e),
                            )

                # Marcar orden como impresa
                printed_orders.add(oid)
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
