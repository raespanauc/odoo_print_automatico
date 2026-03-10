"""
OdooPrintMonitor — Entrypoint principal.
Monitorea albaranes en Odoo y los imprime automáticamente.
"""

import os
import sys
import time
from datetime import datetime, timezone

from loguru import logger

import config
from odoo_client import OdooClient
from printer import ThermalPrinter

# ─── Logging ──────────────────────────────────────────────────────────────────

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logger.remove()  # quitar handler por defecto (stderr)
logger.add(sys.stderr, level=config.LOG_LEVEL)
logger.add(
    os.path.join(LOG_DIR, "monitor_{time:YYYY-MM-DD}.log"),
    rotation="00:00",
    retention="30 days",
    level=config.LOG_LEVEL,
)


# ─── Main loop ────────────────────────────────────────────────────────────────


def main():
    logger.info("Iniciando OdooPrintMonitor...")

    # Validar configuración mínima
    if not config.ODOO_USER or not config.ODOO_APIKEY:
        logger.error("ODOO_USER y ODOO_APIKEY son obligatorios. Revisa el archivo .env")
        sys.exit(1)

    # Inicializar cliente Odoo
    client = OdooClient(
        url=config.ODOO_URL,
        db=config.ODOO_DB,
        user=config.ODOO_USER,
        apikey=config.ODOO_APIKEY,
    )

    try:
        client.authenticate()
        client.get_session_cookie()
    except Exception as e:
        logger.error(f"Error en conexión inicial: {e}")
        sys.exit(1)

    # Inicializar impresora
    try:
        printer = ThermalPrinter(config.PRINTER_NAME)
    except RuntimeError as e:
        logger.error(str(e))
        sys.exit(1)

    # Timestamp de inicio como referencia
    last_check = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    logger.info(f"Monitoreando desde {last_check} cada {config.POLL_INTERVAL_SECS}s")

    # ─── Loop principal ───────────────────────────────────────────────────────
    while True:
        try:
            pickings = client.get_ready_pickings(last_check)

            for picking in pickings:
                pid = picking["id"]
                name = picking["name"]
                logger.info(f"Nuevo albaran: {name} (id={pid}) — imprimiendo...")

                # Descargar PDF
                try:
                    pdf = client.download_pdf(pid, config.REPORT_ACTION)
                except Exception as e:
                    logger.error(f"No se pudo descargar PDF de {name}: {e}")
                    continue

                # Imprimir
                printed = printer.print_pdf(pdf)
                if not printed:
                    logger.error(f"Fallo al imprimir {name} — NO se marca como impreso")
                    continue

                # Marcar como impreso
                try:
                    client.mark_as_printed(pid)
                    logger.info(f"OK {name} impreso correctamente")
                except Exception as e:
                    logger.error(f"Impreso pero no se pudo marcar {name}: {e}")

            # Actualizar timestamp
            last_check = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        except Exception as e:
            logger.warning(f"Error en ciclo de polling: {e}")
            # Intentar reconexión
            try:
                client.refresh_session()
            except Exception as re:
                logger.error(f"Reconexion fallida: {re}")

        time.sleep(config.POLL_INTERVAL_SECS)


if __name__ == "__main__":
    main()
