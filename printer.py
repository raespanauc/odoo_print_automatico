import os
import tempfile
import time
import win32api
import win32print
from loguru import logger


class ThermalPrinter:
    def __init__(self, printer_name: str):
        self.printer_name = printer_name
        self._verify_printer()

    def _verify_printer(self):
        """Verifica que la impresora existe en Windows."""
        printers = [p[2] for p in win32print.EnumPrinters(win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS)]
        if self.printer_name not in printers:
            available = ", ".join(printers)
            raise RuntimeError(
                f"Impresora '{self.printer_name}' no encontrada. "
                f"Disponibles: {available}"
            )
        logger.info(f"Impresora verificada: {self.printer_name}")

    def print_pdf(self, pdf_bytes: bytes) -> bool:
        """Imprime un PDF en la impresora térmica."""
        path = self._save_temp(pdf_bytes)
        try:
            win32api.ShellExecute(
                0,
                "print",
                path,
                f'/d:"{self.printer_name}"',
                ".",
                0,  # SW_HIDE
            )
            # Esperar un momento para que el sistema operativo procese el trabajo
            time.sleep(3)
            return True
        except Exception as e:
            logger.error(f"Error al imprimir: {e}")
            return False
        finally:
            self._cleanup(path)

    def _save_temp(self, pdf_bytes: bytes) -> str:
        """Guarda los bytes del PDF en un archivo temporal."""
        fd, path = tempfile.mkstemp(suffix=".pdf", prefix="odoo_alb_")
        os.write(fd, pdf_bytes)
        os.close(fd)
        logger.debug(f"PDF temporal guardado: {path}")
        return path

    def _cleanup(self, path: str):
        """Elimina el archivo temporal."""
        try:
            if os.path.exists(path):
                os.remove(path)
                logger.debug(f"Temporal eliminado: {path}")
        except OSError as e:
            logger.warning(f"No se pudo eliminar {path}: {e}")

    def test_print(self) -> bool:
        """Imprime una página de prueba."""
        try:
            hprinter = win32print.OpenPrinter(self.printer_name)
            try:
                win32print.StartDocPrinter(hprinter, 1, ("Test OdooPrintMonitor", None, "RAW"))
                win32print.StartPagePrinter(hprinter)
                win32print.WritePrinter(hprinter, b"\n  OdooPrintMonitor\n  Prueba OK\n\n\n\n")
                win32print.EndPagePrinter(hprinter)
                win32print.EndDocPrinter(hprinter)
            finally:
                win32print.ClosePrinter(hprinter)
            logger.info("Pagina de prueba enviada correctamente")
            return True
        except Exception as e:
            logger.error(f"Error en prueba de impresion: {e}")
            return False
