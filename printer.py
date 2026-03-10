import os
import subprocess
import tempfile
import time
import win32print
from loguru import logger


class PrinterManager:
    """Gestiona impresión a múltiples impresoras de red."""

    def __init__(self, printer_names: list[str]):
        self.printers = []
        for name in printer_names:
            name = name.strip()
            if not name:
                continue
            if self._printer_exists(name):
                self.printers.append(name)
                logger.info(f"Impresora verificada: {name}")
            else:
                logger.warning(f"Impresora '{name}' no encontrada en Windows, ignorando")

        if not self.printers:
            raise RuntimeError("No se encontraron impresoras válidas")

    def _printer_exists(self, name: str) -> bool:
        installed = [
            p[2] for p in win32print.EnumPrinters(
                win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
            )
        ]
        return name in installed

    def print_pdf(self, pdf_bytes: bytes, doc_name: str = "OdooPrint") -> bool:
        """Envía el PDF a todas las impresoras configuradas usando SumatraPDF."""
        path = self._save_temp(pdf_bytes)
        all_ok = True
        try:
            for printer_name in self.printers:
                try:
                    self._print_with_sumatra(path, printer_name, doc_name)
                    logger.info(f"Enviado a {printer_name}: {doc_name}")
                except Exception as e:
                    logger.error(f"Error imprimiendo en {printer_name}: {e}")
                    all_ok = False
        finally:
            # Esperar a que el spooler procese antes de limpiar
            time.sleep(5)
            self._cleanup(path)
        return all_ok

    def _print_with_sumatra(self, pdf_path: str, printer_name: str, doc_name: str):
        """Imprime PDF usando SumatraPDF (silencioso, no necesita visor instalado)."""
        sumatra = self._get_sumatra_path()
        cmd = [
            sumatra,
            "-print-to", printer_name,
            "-silent",
            "-print-settings", "fit",
            pdf_path,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(
                f"SumatraPDF retornó código {result.returncode}: "
                f"{result.stderr.decode(errors='ignore')}"
            )

    def _get_sumatra_path(self) -> str:
        """Busca SumatraPDF en el proyecto o en PATH."""
        # Buscar en el directorio del proyecto
        base = os.path.dirname(os.path.abspath(__file__))
        local_path = os.path.join(base, "tools", "SumatraPDF.exe")
        if os.path.exists(local_path):
            return local_path
        # Buscar en PATH
        for path_dir in os.environ.get("PATH", "").split(";"):
            candidate = os.path.join(path_dir, "SumatraPDF.exe")
            if os.path.exists(candidate):
                return candidate
        raise FileNotFoundError(
            "SumatraPDF no encontrado. Descárgalo de https://www.sumatrapdfreader.org/download-free-pdf-viewer "
            f"y colócalo en {os.path.join(base, 'tools', 'SumatraPDF.exe')}"
        )

    def _save_temp(self, pdf_bytes: bytes) -> str:
        fd, path = tempfile.mkstemp(suffix=".pdf", prefix="odoo_print_")
        os.write(fd, pdf_bytes)
        os.close(fd)
        return path

    def _cleanup(self, path: str):
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError as e:
            logger.warning(f"No se pudo eliminar temporal {path}: {e}")
