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
_ZONE_RE = re.compile(r"[/\- ](Z\d+)(?:[/\- ]|$)", re.IGNORECASE)


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

    # Inicializar cliente Odoo desde settings (SQLite > env vars)
    def connect_odoo(s=None):
        """Crea y autentica un OdooClient con los settings actuales."""
        if s is None:
            s = store.get_odoo_settings()
        c = OdooClient(
            url=s["odoo_url"],
            db=s["odoo_db"],
            user=s["odoo_user"],
            password=s["odoo_password"],
        )
        c.authenticate()
        c.get_session_cookie()
        update_state(odoo_url=s["odoo_url"], odoo_db=s["odoo_db"])
        return c, s

    try:
        client, current_settings = connect_odoo()
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
            # Detectar cambio de settings → reconectar Odoo
            new_settings = store.get_odoo_settings()
            if (new_settings["odoo_url"] != current_settings["odoo_url"]
                    or new_settings["odoo_db"] != current_settings["odoo_db"]
                    or new_settings["odoo_user"] != current_settings["odoo_user"]
                    or new_settings["odoo_password"] != current_settings["odoo_password"]):
                logger.info(
                    f"Settings cambiados, reconectando a "
                    f"{new_settings['odoo_url']} / {new_settings['odoo_db']}"
                )
                try:
                    client, current_settings = connect_odoo(new_settings)
                    logger.info("Reconexion exitosa con nuevos settings")
                except Exception as e:
                    logger.error(f"Error reconectando con nuevos settings: {e}")

            orders = client.get_confirmed_orders(last_check)
            update_state(last_poll=datetime.now(timezone.utc).isoformat())

            for order in orders:
                oid = order["id"]
                oname = order["name"]

                if oid in printed_orders:
                    continue

                logger.info(f"Nuevo pedido confirmado: {oname} (id={oid})")
                update_state(last_order=oname)

                # Descargar PDF del presupuesto
                try:
                    presupuesto_pdf = client.download_pdf(
                        oid, current_settings["report_presupuesto"],
                    )
                except Exception as e:
                    logger.error(f"Error PDF presupuesto {oname}: {e}")
                    store.record_print(
                        "presupuesto", oname, oid, "N/A", "error", str(e),
                    )
                    continue

                # Obtener albaranes y agrupar por zona
                picking_ids = order.get("picking_ids", [])
                pickings = []
                if picking_ids:
                    pickings = client.get_pickings_by_ids(picking_ids)

                # Agrupar albaranes por zona para ruteo a impresoras
                zone_pickings = {}  # zona -> [pick, ...]
                for pick in pickings:
                    if pick["id"] in printed_pickings:
                        continue
                    zone = extract_zone(pick["name"])
                    if zone:
                        zone_pickings.setdefault(zone, []).append(pick)
                    else:
                        logger.warning(
                            f"Albaran {pick['name']} sin zona detectada, "
                            f"no se imprimirá"
                        )

                if not zone_pickings:
                    logger.warning(
                        f"Pedido {oname} sin albaranes con zona asignada, "
                        f"no se imprime"
                    )
                    printed_orders.add(oid)
                    continue

                # Para cada zona: imprimir presupuesto + albaranes
                # en la impresora asignada a esa zona
                any_order_ok = False
                for zone, picks in zone_pickings.items():
                    zone_printers = store.get_printers_for_zone(zone)
                    if not zone_printers:
                        logger.warning(
                            f"Zona {zone}: sin impresora asignada, "
                            f"saltando {len(picks)} albaran(es)"
                        )
                        continue

                    logger.info(
                        f"Zona {zone}: imprimiendo en "
                        f"{[p['name'] for p in zone_printers]}"
                    )

                    # Imprimir presupuesto en esta zona
                    results = printer.print_pdf(
                        presupuesto_pdf,
                        doc_name=f"Presupuesto {oname}",
                        zone=zone,
                    )
                    for r in results:
                        store.record_print(
                            "presupuesto", oname, oid, r["printer"],
                            r["status"], r.get("error"),
                        )
                        if r["status"] == "ok":
                            any_order_ok = True

                    time.sleep(3)

                    # Imprimir albaranes de esta zona
                    for pick in picks:
                        pid = pick["id"]
                        pname = pick["name"]
                        logger.info(f"Albaran {pname} → zona {zone}")

                        try:
                            albaran_pdf = client.download_pdf(
                                pid, current_settings["report_albaran"],
                            )
                            results = printer.print_pdf(
                                albaran_pdf,
                                doc_name=f"Albaran {pname}",
                                zone=zone,
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
                if any_order_ok:
                    logger.info(f"OK {oname} procesado completamente")
                else:
                    logger.warning(f"{oname} procesado pero sin impresiones exitosas")

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
