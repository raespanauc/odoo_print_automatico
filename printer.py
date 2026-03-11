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
    """Gestiona impresión a múltiples impresoras de red.
    Carga impresoras desde PrintStore (SQLite) en cada impresión."""

    def __init__(self, store):
        self.store = store
        printers = store.get_printers(only_enabled=True)
        for p in printers:
            try:
                s = socket.create_connection((p["ip"], p["port"]), timeout=5)
                s.close()
                logger.info(f"Impresora verificada (red): {p['name']} ({p['ip']}:{p['port']})")
            except Exception as e:
                logger.warning(f"Impresora {p['name']} ({p['ip']}:{p['port']}) no accesible al inicio: {e}")

        if not printers:
            logger.warning("No hay impresoras configuradas en la base de datos")

    def print_pdf(self, pdf_bytes: bytes, doc_name: str = "OdooPrint",
                  zone: str = None) -> list:
        """Envía el PDF a impresoras. Si zone se especifica, solo a las de esa zona."""
        if zone:
            printers = self.store.get_printers_for_zone(zone)
            if not printers:
                logger.warning(f"No hay impresoras asignadas a zona {zone}")
                return []
            logger.info(f"Zona {zone}: imprimiendo en {[p['name'] for p in printers]}")
        else:
            printers = self.store.get_printers(only_enabled=True)
            if not printers:
                logger.warning("No hay impresoras habilitadas para imprimir")
                return []

        path = self._save_temp(pdf_bytes)
        results = []
        try:
            for p in printers:
                try:
                    if IS_WINDOWS:
                        self._print_windows(path, p["name"])
                    else:
                        self._print_escpos(path, p["name"], p["ip"], p["port"])
                    logger.info(f"Enviado a {p['name']}: {doc_name}")
                    results.append({"printer": p["name"], "status": "ok"})
                except Exception as e:
                    logger.error(f"Error imprimiendo en {p['name']}: {e}")
                    results.append({
                        "printer": p["name"],
                        "status": "error",
                        "error": str(e),
                    })
        finally:
            self._cleanup(path)
        return results

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

    def _print_escpos(self, pdf_path: str, printer_name: str, ip: str = None, port: int = 9100):
        """Convierte PDF a imagen y envía por ESC/POS al puerto 9100."""
        if not ip:
            raise RuntimeError(f"No hay IP para impresora {printer_name}")

        tmpdir = tempfile.mkdtemp(prefix="odoo_img_")
        try:
            # 1. Convertir PDF a imágenes PNG
            subprocess.run([
                "pdftoppm", "-png", "-r", "203",
                "-cropbox",
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

            with socket.create_connection((ip, port), timeout=60) as sock:
                sock.sendall(ESCPOS_INIT)

                for page_path in pages:
                    img = Image.open(page_path).convert("L")
                    logger.info(f"Imagen original: {img.width}x{img.height}px")

                    # Redimensionar al ancho exacto
                    if img.width != THERMAL_WIDTH_PX:
                        ratio = THERMAL_WIDTH_PX / img.width
                        img = img.resize((THERMAL_WIDTH_PX, int(img.height * ratio)))

                    # Recortar espacio en blanco inferior
                    img = self._trim_bottom(img)
                    logger.info(f"Imagen recortada: {img.width}x{img.height}px")

                    # Convertir a 1-bit blanco y negro
                    img = img.point(lambda x: 0 if x < 128 else 255, "1")

                    # Enviar en bandas para no desbordar buffer
                    self._send_image_bands(sock, img)

                    # Cortar papel después de cada página
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
        """Recorta espacio en blanco inferior. Detecta gap entre contenido y footer."""
        width = img.width
        height = img.height
        data = img.tobytes()

        threshold = 200
        min_dark_pixels = max(1, int(width * 0.01))

        # Clasificar cada fila como contenido (True) o blanca (False)
        has_content = []
        for y in range(height):
            row_start = y * width
            row = data[row_start:row_start + width]
            dark_count = sum(1 for b in row if b < threshold)
            has_content.append(dark_count >= min_dark_pixels)

        # Buscar desde arriba el último bloque de contenido antes de un gap grande
        # Un gap de >300 filas blancas indica separación contenido/footer
        GAP_THRESHOLD = 300
        gap_start = None
        consecutive_white = 0
        main_content_end = height

        for y in range(height):
            if not has_content[y]:
                if consecutive_white == 0:
                    gap_start = y
                consecutive_white += 1
            else:
                if consecutive_white >= GAP_THRESHOLD:
                    # Hay un gap grande antes de este contenido → esto es footer
                    main_content_end = gap_start
                    logger.info(
                        f"Trim: gap de {consecutive_white} filas blancas "
                        f"en fila {gap_start}, footer detectado"
                    )
                    break
                consecutive_white = 0

        # Si no se encontró gap, buscar la última fila con contenido
        if main_content_end == height:
            for y in range(height - 1, -1, -1):
                if has_content[y]:
                    main_content_end = y + 1
                    break

        crop_h = min(main_content_end + 40, height)
        if crop_h < height - 50:
            logger.info(
                f"Trim: recortando a {crop_h}px de {height}px original"
            )
            return img.crop((0, 0, width, crop_h))

        logger.info(f"Trim: contenido ocupa {main_content_end} de {height}px, sin recortar")
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
