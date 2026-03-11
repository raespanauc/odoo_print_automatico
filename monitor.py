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

    # Inicializar store y migrar datos legacy
    store = PrintStore()
    for legacy in (LEGACY_FILE, LEGACY_FILE_ALT):
        if os.path.exists(legacy):
            count = store.import_from_json(legacy)
            if count:
                logger.info(f"Migrados {count} registros desde {legacy}")
            # Renombrar para no re-importar
            os.rename(legacy, legacy + ".bak")

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
    printer = PrinterManager(config.PRINTER_NAMES, config.PRINTER_IPS)

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
                            pdf = client.download_pdf(pid, config.REPORT_ALBARAN)
                            results = printer.print_pdf(pdf, doc_name=f"Albaran {pname}")
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
