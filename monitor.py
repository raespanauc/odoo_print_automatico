"""
OdooPrintMonitor — Entrypoint principal.
Monitorea presupuestos confirmados en Odoo y los imprime automáticamente
en las impresoras según la zona de cada albarán.
"""

import os
import sys
import tempfile
import time
from datetime import datetime, timezone

from loguru import logger

import config
from odoo_client import OdooClient
from printer import PrinterManager, detect_page_zones
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
    logger.info(f"Registro: {len(printed_orders)} ordenes previamente impresas")

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

                # Descargar PDF combinado (presupuesto + albaranes)
                try:
                    combined_pdf = client.download_pdf(
                        oid, current_settings["report_presupuesto"],
                    )
                except Exception as e:
                    logger.error(f"Error descargando PDF {oname}: {e}")
                    store.record_print(
                        "presupuesto", oname, oid, "N/A", "error", str(e),
                    )
                    continue

                # Guardar PDF temporal para análisis de zonas
                fd, pdf_path = tempfile.mkstemp(suffix=".pdf", prefix="odoo_zones_")
                os.write(fd, combined_pdf)
                os.close(fd)

                try:
                    # Detectar zona de cada página
                    page_zones = detect_page_zones(pdf_path)

                    # Separar: páginas de presupuesto vs albaranes por zona
                    presupuesto_pages = [i for i, z in page_zones.items() if z is None]
                    zone_pages = {}  # zone -> [page_indices]
                    for i, z in page_zones.items():
                        if z:
                            zone_pages.setdefault(z, []).append(i)

                    if not zone_pages:
                        logger.warning(
                            f"Pedido {oname}: no se detectaron zonas en el PDF, "
                            f"no se imprime"
                        )
                        printed_orders.add(oid)
                        continue

                    logger.info(
                        f"Pedido {oname}: presupuesto={presupuesto_pages}, "
                        f"zonas={zone_pages}"
                    )

                    # Para cada zona: imprimir presupuesto + albarán de esa zona
                    any_ok = False
                    for zone, albaran_pages in zone_pages.items():
                        zone_printers = store.get_printers_for_zone(zone)
                        if not zone_printers:
                            logger.warning(
                                f"Zona {zone}: sin impresora asignada, "
                                f"saltando páginas {albaran_pages}"
                            )
                            continue

                        # Páginas a imprimir: presupuesto + albarán(es) de esta zona
                        pages_to_print = sorted(presupuesto_pages + albaran_pages)
                        logger.info(
                            f"Zona {zone} → {[p['name'] for p in zone_printers]}: "
                            f"páginas {pages_to_print}"
                        )

                        results = printer.print_pdf(
                            combined_pdf,
                            doc_name=f"{oname} (Z{zone})",
                            zone=zone,
                            pages=pages_to_print,
                        )
                        for r in results:
                            store.record_print(
                                "pedido", oname, oid, r["printer"],
                                r["status"], r.get("error"),
                            )
                            if r["status"] == "ok":
                                any_ok = True

                        time.sleep(3)

                    # Marcar orden como impresa
                    printed_orders.add(oid)
                    if any_ok:
                        logger.info(f"OK {oname} procesado completamente")
                    else:
                        logger.warning(f"{oname} procesado pero sin impresiones exitosas")

                finally:
                    # Limpiar PDF temporal
                    try:
                        os.remove(pdf_path)
                    except OSError:
                        pass

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
