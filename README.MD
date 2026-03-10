# OdooPrintMonitor

Servicio Windows local que monitorea albaranes de entrega en Odoo SaaS y los imprime automáticamente en una impresora térmica USB de 80mm, sin depender del IoT Box de Odoo.

## Problema que resuelve

El IoT Box virtual de Odoo SaaS introduce latencias de 5–30 segundos porque el websocket pasa por servidores remotos. Esta app corre localmente, hace polling directo a la API de Odoo cada N segundos y envía el PDF a la impresora local sin intermediarios.

## Stack

- **Lenguaje:** Python 3.11+
- **SO destino:** Windows 10/11
- **Impresora:** EPSON TM-T20III (USB, 80mm) — nombre en Windows: `EPSON TM-T20II TOTEM`
- **Odoo:** 18 SaaS — `https://repuestosespana.odoo.com` / DB: `repuestosespana`
- **Instalación como servicio:** NSSM

## Dependencias Python

```
requests
pywin32
python-dotenv
loguru
```

## Estructura del proyecto

```
odoo-print-monitor/
├── README.md
├── .env.example
├── .env                  # NO commitear — credenciales
├── requirements.txt
├── monitor.py            # entrypoint principal
├── config.py             # carga variables de entorno
├── odoo_client.py        # wrapper XML-RPC + sesión web
├── printer.py            # lógica de impresión win32
├── install_service.ps1   # script PowerShell para instalar como servicio Windows
└── logs/                 # directorio de logs (autogenerado)
```

## Variables de entorno (.env)

```env
ODOO_URL=https://repuestosespana.odoo.com
ODOO_DB=repuestosespana
ODOO_USER=tu@email.com
ODOO_APIKEY=tu_api_key_aqui
PRINTER_NAME=EPSON TM-T20II TOTEM
POLL_INTERVAL_SECS=8
REPORT_ACTION=stock.report_deliveryslip80
LOG_LEVEL=INFO
```

> La API Key se obtiene en Odoo: Ajustes → Mi perfil → Seguridad de la cuenta → Claves API

## Flujo de la aplicación

```
1. Al iniciar:
   - Carga config desde .env
   - Autentica vía XML-RPC (obtiene uid)
   - Abre sesión web (obtiene session_id para descargar PDFs)
   - Registra timestamp de inicio como last_check

2. Loop cada POLL_INTERVAL_SECS:
   - Consulta stock.picking donde:
       state = 'assigned'          (Preparado / Ready)
       picking_type_code = 'outgoing'
       write_date >= last_check
       x_studio_printed != True   (campo custom en Odoo Studio)
   - Por cada albarán nuevo:
       a. Descarga PDF via /report/pdf/stock.report_deliveryslip80/{id}
       b. Guarda en archivo temporal
       c. Envía a impresora vía win32api.ShellExecute con acción "print"
       d. Escribe x_studio_printed = True en el registro de Odoo
       e. Elimina el archivo temporal
       f. Registra en log
   - Actualiza last_check
   - Maneja errores con reconexión automática
```

## Campo personalizado requerido en Odoo

Antes de ejecutar la app, crear el siguiente campo en Odoo Studio:

| Propiedad | Valor |
|-----------|-------|
| Modelo | `stock.picking` (Transferencias) |
| Nombre técnico | `x_studio_printed` |
| Tipo | Booleano |
| Valor por defecto | `False` |
| Etiqueta | `Impreso automáticamente` |

Esto evita que un albarán se imprima más de una vez si es editado.

## Instalación manual

```powershell
# 1. Clonar o copiar el proyecto
cd C:\Desarrollos
git clone ... odoo-print-monitor
cd odoo-print-monitor

# 2. Crear entorno virtual
python -m venv venv
.\venv\Scripts\Activate.ps1

# 3. Instalar dependencias
pip install -r requirements.txt

# 4. Configurar credenciales
copy .env.example .env
# Editar .env con credenciales reales

# 5. Probar ejecución manual
python monitor.py
```

## Instalación como servicio Windows (NSSM)

El archivo `install_service.ps1` debe:

1. Verificar que NSSM está instalado (o descargarlo de https://nssm.cc)
2. Instalar el servicio apuntando al entorno virtual:
   ```powershell
   nssm install OdooPrintMonitor "C:\Desarrollos\odoo-print-monitor\venv\Scripts\python.exe"
   nssm set OdooPrintMonitor AppParameters "C:\Desarrollos\odoo-print-monitor\monitor.py"
   nssm set OdooPrintMonitor AppDirectory "C:\Desarrollos\odoo-print-monitor"
   nssm set OdooPrintMonitor AppStdout "C:\Desarrollos\odoo-print-monitor\logs\service.log"
   nssm set OdooPrintMonitor AppStderr "C:\Desarrollos\odoo-print-monitor\logs\service_error.log"
   nssm set OdooPrintMonitor Start SERVICE_AUTO_START
   Start-Service OdooPrintMonitor
   ```

## Módulo odoo_client.py — spec

```python
class OdooClient:
    def __init__(self, url, db, user, apikey)
    def authenticate() -> int                          # retorna uid
    def get_session_cookie() -> str                    # sesión web para PDF
    def refresh_session()                              # reconectar si expira
    def get_ready_pickings(last_check: str) -> list    # XML-RPC search_read
    def download_pdf(picking_id: int) -> bytes         # GET /report/pdf/...
    def mark_as_printed(picking_id: int) -> bool       # XML-RPC write
```

## Módulo printer.py — spec

```python
class ThermalPrinter:
    def __init__(self, printer_name: str)
    def print_pdf(pdf_bytes: bytes) -> bool
    def _save_temp(pdf_bytes: bytes) -> str    # guarda en tempfile
    def _cleanup(path: str)                   # elimina temp
    def test_print() -> bool                  # imprime página de prueba
```

## Manejo de errores

| Error | Comportamiento |
|-------|---------------|
| Fallo de red / timeout | Reintentar en próximo ciclo, loguear warning |
| Sesión Odoo expirada (401) | Reconectar automáticamente antes del siguiente ciclo |
| Impresora no disponible | Loguear error, NO marcar como impreso, reintentar |
| PDF no disponible (404) | Loguear error, marcar para revisión manual |
| Error XML-RPC | Reconectar, si persiste → alerta en log |

## Logging

Usar `loguru` con rotación diaria:

```python
logger.add("logs/monitor_{time:YYYY-MM-DD}.log",
           rotation="00:00",
           retention="30 days",
           level=LOG_LEVEL)
```

Formato sugerido:
```
2025-03-10 14:32:01 | INFO | Iniciando OdooPrintMonitor...
2025-03-10 14:32:05 | INFO | Conectado a repuestosespana.odoo.com (uid=5)
2025-03-10 14:32:13 | INFO | Nuevo albarán: OUT/00234 (id=1821) — imprimiendo...
2025-03-10 14:32:15 | INFO | ✓ OUT/00234 impreso correctamente
2025-03-10 14:32:23 | WARNING | Reintentando conexión...
```

## Consideraciones de seguridad

- El archivo `.env` nunca debe commitearse — agregar al `.gitignore`
- La API Key de Odoo debe tener permisos mínimos: lectura de `stock.picking`, escritura de `x_studio_printed`, descarga de reportes
- Crear un usuario Odoo dedicado `print-monitor@repuestosespana.cl` con perfil de acceso restringido

## Contexto técnico del entorno

- **PC:** `respana_pc` — Windows 11 — IP `10.69.1.171`
- **Impresora:** EPSON TM-T20IIIL USB — 203dpi — 80mm
- **Nombre Windows de la impresora:** `EPSON TM-T20II TOTEM`
- **Odoo:** v18 SaaS — `repuestosespana.odoo.com`
- **Reporte albarán 80mm:** `stock.report_deliveryslip80`
- **Directorio de desarrollo:** `C:\Desarrollos\`

## Posibles mejoras futuras

- [ ] Soporte para múltiples impresoras por tipo de operación
- [ ] Webhook en lugar de polling (requiere IP pública o túnel)
- [ ] Interfaz tray de Windows (icono en bandeja del sistema)
- [ ] Impresión desde Zebra TC15 vía red al mismo monitor
- [ ] Reporte ESC/POS nativo para evitar renderizado PDF