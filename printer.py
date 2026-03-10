import os
import platform
import subprocess
import tempfile
import time
from loguru import logger

IS_WINDOWS = platform.system() == "Windows"

if IS_WINDOWS:
    import win32print


class PrinterManager:
    """Gestiona impresión a múltiples impresoras de red."""

    def __init__(self, printer_names: list[str], printer_ips: dict[str, str] = None):
        """
        printer_names: lista de nombres de impresora
        printer_ips: dict {nombre: ip} para configurar en CUPS (Linux)
        """
        self.printers = []
        self.printer_ips = printer_ips or {}

        if IS_WINDOWS:
            self._init_windows(printer_names)
        else:
            self._init_linux(printer_names)

        if not self.printers:
            raise RuntimeError("No se encontraron impresoras válidas")

    def _init_windows(self, printer_names):
        installed = [
            p[2] for p in win32print.EnumPrinters(
                win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
            )
        ]
        for name in printer_names:
            name = name.strip()
            if name in installed:
                self.printers.append(name)
                logger.info(f"Impresora verificada (Windows): {name}")
            else:
                logger.warning(f"Impresora '{name}' no encontrada en Windows")

    def _init_linux(self, printer_names):
        for name in printer_names:
            name = name.strip()
            if not name:
                continue
            ip = self.printer_ips.get(name)
            if ip:
                self._add_cups_printer(name, ip)
            self.printers.append(name)
            logger.info(f"Impresora configurada (CUPS): {name}")

    def _add_cups_printer(self, name: str, ip: str):
        """Agrega impresora a CUPS si no existe."""
        try:
            # Verificar si ya existe
            result = subprocess.run(
                ["lpstat", "-p", name],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                return
            # Agregar impresora RAW en puerto 9100
            subprocess.run([
                "lpadmin", "-p", name,
                "-E",
                "-v", f"socket://{ip}:9100",
                "-m", "raw",
            ], capture_output=True, timeout=10, check=True)
            logger.info(f"Impresora {name} agregada a CUPS ({ip}:9100)")
        except Exception as e:
            logger.warning(f"No se pudo agregar {name} a CUPS: {e}")

    def print_pdf(self, pdf_bytes: bytes, doc_name: str = "OdooPrint") -> bool:
        """Envía el PDF a todas las impresoras configuradas."""
        path = self._save_temp(pdf_bytes)
        all_ok = True
        try:
            for printer_name in self.printers:
                try:
                    if IS_WINDOWS:
                        self._print_windows(path, printer_name)
                    else:
                        self._print_linux(path, printer_name)
                    logger.info(f"Enviado a {printer_name}: {doc_name}")
                except Exception as e:
                    logger.error(f"Error imprimiendo en {printer_name}: {e}")
                    all_ok = False
        finally:
            time.sleep(3)
            self._cleanup(path)
        return all_ok

    def _print_windows(self, pdf_path: str, printer_name: str):
        """Imprime via SumatraPDF en Windows."""
        sumatra = self._get_sumatra_path()
        result = subprocess.run([
            sumatra,
            "-print-to", printer_name,
            "-silent",
            "-print-settings", "fit",
            pdf_path,
        ], capture_output=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(
                f"SumatraPDF código {result.returncode}: "
                f"{result.stderr.decode(errors='ignore')}"
            )

    def _print_linux(self, pdf_path: str, printer_name: str):
        """Imprime via lp (CUPS) en Linux."""
        result = subprocess.run([
            "lp", "-d", printer_name,
            "-o", "fit-to-page",
            pdf_path,
        ], capture_output=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(
                f"lp código {result.returncode}: "
                f"{result.stderr.decode(errors='ignore')}"
            )

    def _get_sumatra_path(self) -> str:
        base = os.path.dirname(os.path.abspath(__file__))
        local_path = os.path.join(base, "tools", "SumatraPDF.exe")
        if os.path.exists(local_path):
            return local_path
        for path_dir in os.environ.get("PATH", "").split(";"):
            candidate = os.path.join(path_dir, "SumatraPDF.exe")
            if os.path.exists(candidate):
                return candidate
        raise FileNotFoundError(
            "SumatraPDF no encontrado. Colócalo en tools/SumatraPDF.exe"
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
