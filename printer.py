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
ESCPOS_INIT = ESC + b'@'
ESCPOS_CUT = GS + b'V\x00'
ESCPOS_FEED = ESC + b'd\x05'

# Ancho en pixels para 80mm a 203dpi
THERMAL_WIDTH_PX = 576
# Altura máxima por banda (evita desbordar buffer de impresora)
BAND_HEIGHT = 256


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

        tmpdir = tempfile.mkdtemp(prefix="odoo_img_")
        try:
            # 1. Convertir PDF a imágenes PNG
            subprocess.run([
                "pdftoppm", "-png", "-r", "203",
                "-scale-to-x", str(THERMAL_WIDTH_PX),
                "-scale-to-y", "-1",
                pdf_path,
                os.path.join(tmpdir, "page"),
            ], capture_output=True, timeout=30, check=True)

            pages = sorted([
                os.path.join(tmpdir, f) for f in os.listdir(tmpdir)
                if f.endswith(".png")
            ])
            if not pages:
                raise RuntimeError("pdftoppm no generó imágenes")

            # 2. Procesar y enviar por socket
            from PIL import Image

            with socket.create_connection((ip, 9100), timeout=30) as sock:
                sock.sendall(ESCPOS_INIT)

                for page_path in pages:
                    img = Image.open(page_path).convert("L")

                    # Redimensionar al ancho exacto
                    if img.width != THERMAL_WIDTH_PX:
                        ratio = THERMAL_WIDTH_PX / img.width
                        img = img.resize((THERMAL_WIDTH_PX, int(img.height * ratio)))

                    # Recortar espacio en blanco inferior
                    img = self._trim_bottom(img)
                    logger.debug(f"Imagen recortada: {img.width}x{img.height}px")

                    # Convertir a 1-bit blanco y negro
                    img = img.point(lambda x: 0 if x < 128 else 255, "1")

                    # Enviar en bandas para no desbordar buffer
                    self._send_image_bands(sock, img)

                sock.sendall(ESCPOS_FEED + ESCPOS_CUT)

        finally:
            for f in os.listdir(tmpdir):
                os.remove(os.path.join(tmpdir, f))
            os.rmdir(tmpdir)

    def _send_image_bands(self, sock, img):
        """Envía la imagen en bandas de BAND_HEIGHT filas."""
        width_bytes = (img.width + 7) // 8
        total_height = img.height

        for y in range(0, total_height, BAND_HEIGHT):
            band_h = min(BAND_HEIGHT, total_height - y)
            band = img.crop((0, y, img.width, y + band_h))
            pixels = band.tobytes()

            # Invertir bits: ESC/POS 1=negro, PIL mode "1" 0=negro
            inverted = bytes(~b & 0xFF for b in pixels)

            # GS v 0 m xL xH yL yH data
            header = GS + b'v0\x00'
            header += struct.pack('<H', width_bytes)
            header += struct.pack('<H', band_h)

            sock.sendall(header + inverted)
            time.sleep(0.05)  # Pausa entre bandas para la impresora

    def _trim_bottom(self, img):
        """Recorta el espacio en blanco inferior."""
        # Escanear desde abajo buscando la última fila con contenido
        pixels = img.load()
        last_content_row = 0

        for y in range(img.height - 1, -1, -1):
            for x in range(0, img.width, 4):  # Muestrear cada 4 pixels
                if pixels[x, y] < 240:  # No es blanco puro
                    last_content_row = y
                    break
            if last_content_row > 0:
                break

        if last_content_row > 0:
            # Agregar margen de 40px después del contenido
            crop_h = min(last_content_row + 40, img.height)
            return img.crop((0, 0, img.width, crop_h))
        return img

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
