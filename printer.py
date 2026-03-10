import os
import platform
import socket
import struct
import subprocess
import tempfile
import time
from loguru import logger

IS_WINDOWS = platform.system() == "Windows"

if IS_WINDOWS:
    import win32print


# ─── Constantes ESC/POS ──────────────────────────────────────────────────────

ESC = b'\x1b'
GS = b'\x1d'
ESCPOS_INIT = ESC + b'@'          # Inicializar impresora
ESCPOS_CUT = GS + b'V\x00'       # Corte total de papel
ESCPOS_FEED = ESC + b'd\x03'     # Avanzar 3 líneas

# Ancho en pixels para 80mm a 203dpi
THERMAL_WIDTH_PX = 576


class PrinterManager:
    """Gestiona impresión a múltiples impresoras de red."""

    def __init__(self, printer_names: list[str], printer_ips: dict[str, str] = None):
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
                # Verificar conexión al puerto 9100
                try:
                    s = socket.create_connection((ip, 9100), timeout=5)
                    s.close()
                    self.printers.append(name)
                    logger.info(f"Impresora verificada (red): {name} ({ip}:9100)")
                except Exception as e:
                    logger.warning(f"Impresora {name} ({ip}:9100) no accesible: {e}")
            else:
                logger.warning(f"Impresora {name} sin IP configurada en PRINTER_IPS")

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
                        self._print_escpos(path, printer_name)
                    logger.info(f"Enviado a {printer_name}: {doc_name}")
                except Exception as e:
                    logger.error(f"Error imprimiendo en {printer_name}: {e}")
                    all_ok = False
        finally:
            self._cleanup(path)
        return all_ok

    # ─── Windows: SumatraPDF ──────────────────────────────────────────────────

    def _print_windows(self, pdf_path: str, printer_name: str):
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
        time.sleep(3)

    def _get_sumatra_path(self) -> str:
        base = os.path.dirname(os.path.abspath(__file__))
        local_path = os.path.join(base, "tools", "SumatraPDF.exe")
        if os.path.exists(local_path):
            return local_path
        for path_dir in os.environ.get("PATH", "").split(";"):
            candidate = os.path.join(path_dir, "SumatraPDF.exe")
            if os.path.exists(candidate):
                return candidate
        raise FileNotFoundError("SumatraPDF no encontrado en tools/SumatraPDF.exe")

    # ─── Linux: PDF → imagen → ESC/POS por socket TCP 9100 ───────────────────

    def _print_escpos(self, pdf_path: str, printer_name: str):
        """Convierte PDF a imagen y envía por ESC/POS al puerto 9100."""
        ip = self.printer_ips.get(printer_name)
        if not ip:
            raise RuntimeError(f"No hay IP para impresora {printer_name}")

        # 1. Convertir PDF a imágenes PNG con pdftoppm
        tmpdir = tempfile.mkdtemp(prefix="odoo_img_")
        try:
            subprocess.run([
                "pdftoppm",
                "-png",
                "-r", "203",              # 203 DPI (resolución de impresora térmica)
                "-scale-to-x", str(THERMAL_WIDTH_PX),
                "-scale-to-y", "-1",       # mantener proporción
                pdf_path,
                os.path.join(tmpdir, "page"),
            ], capture_output=True, timeout=30, check=True)

            # 2. Obtener páginas generadas (ordenadas)
            pages = sorted([
                os.path.join(tmpdir, f) for f in os.listdir(tmpdir)
                if f.endswith(".png")
            ])

            if not pages:
                raise RuntimeError("pdftoppm no generó imágenes")

            # 3. Convertir cada página a datos ESC/POS y enviar
            from PIL import Image
            escpos_data = bytearray(ESCPOS_INIT)

            for page_path in pages:
                img = Image.open(page_path)
                img = img.convert("L")  # Escala de grises
                # Redimensionar al ancho exacto si es necesario
                if img.width != THERMAL_WIDTH_PX:
                    ratio = THERMAL_WIDTH_PX / img.width
                    new_h = int(img.height * ratio)
                    img = img.resize((THERMAL_WIDTH_PX, new_h))
                # Convertir a blanco y negro (1 bit)
                img = img.point(lambda x: 0 if x < 128 else 255, "1")
                escpos_data.extend(self._image_to_escpos(img))

            escpos_data.extend(ESCPOS_FEED)
            escpos_data.extend(ESCPOS_CUT)

            # 4. Enviar por TCP al puerto 9100
            with socket.create_connection((ip, 9100), timeout=10) as sock:
                sock.sendall(bytes(escpos_data))

        finally:
            # Limpiar temporales
            for f in os.listdir(tmpdir):
                os.remove(os.path.join(tmpdir, f))
            os.rmdir(tmpdir)

    def _image_to_escpos(self, img) -> bytes:
        """Convierte imagen PIL 1-bit a comandos ESC/POS raster (GS v 0)."""
        width_bytes = (img.width + 7) // 8  # bytes por línea
        height = img.height
        pixels = img.tobytes()

        # GS v 0 — Print raster bit image
        # Format: GS v 0 m xL xH yL yH d1...dk
        # m=0 (normal), xL xH = width in bytes, yL yH = height in dots
        header = GS + b'v0' + b'\x00'
        header += struct.pack('<H', width_bytes)
        header += struct.pack('<H', height)

        # Invertir bits: en ESC/POS, 1=negro, en PIL mode "1", 0=negro
        inverted = bytes(~b & 0xFF for b in pixels)

        return header + inverted

    # ─── Utilidades ───────────────────────────────────────────────────────────

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
