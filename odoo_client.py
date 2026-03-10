import xmlrpc.client
import requests
from loguru import logger


class OdooClient:
    def __init__(self, url: str, db: str, user: str, apikey: str):
        self.url = url.rstrip("/")
        self.db = db
        self.user = user
        self.apikey = apikey
        self.uid = None
        self.session_id = None
        self._common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        self._models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")
        self._web_session = requests.Session()

    def authenticate(self) -> int:
        """Autentica vía XML-RPC y retorna uid."""
        self.uid = self._common.authenticate(self.db, self.user, self.apikey, {})
        if not self.uid:
            raise ConnectionError(f"No se pudo autenticar como {self.user}")
        logger.info(f"Conectado a {self.url} (uid={self.uid})")
        return self.uid

    def get_session_cookie(self) -> str:
        """Abre sesión web para poder descargar PDFs vía /report/pdf/."""
        resp = self._web_session.post(
            f"{self.url}/web/session/authenticate",
            json={
                "jsonrpc": "2.0",
                "params": {
                    "db": self.db,
                    "login": self.user,
                    "password": self.apikey,
                },
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            raise ConnectionError(f"Error al abrir sesión web: {data['error']}")
        self.session_id = self._web_session.cookies.get("session_id")
        logger.debug(f"Sesión web obtenida: session_id={self.session_id[:8]}...")
        return self.session_id

    def refresh_session(self):
        """Reconecta XML-RPC y sesión web."""
        logger.info("Reconectando sesión...")
        self._common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        self._models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")
        self._web_session = requests.Session()
        self.authenticate()
        self.get_session_cookie()

    def get_ready_pickings(self, last_check: str) -> list:
        """Busca albaranes listos para imprimir desde last_check."""
        domain = [
            ["state", "=", "assigned"],
            ["picking_type_code", "=", "outgoing"],
            ["write_date", ">=", last_check],
            ["x_studio_printed", "!=", True],
        ]
        fields = ["id", "name", "write_date", "partner_id"]
        pickings = self._models.execute_kw(
            self.db,
            self.uid,
            self.apikey,
            "stock.picking",
            "search_read",
            [domain],
            {"fields": fields, "order": "write_date asc"},
        )
        return pickings

    def download_pdf(self, picking_id: int, report_action: str) -> bytes:
        """Descarga el PDF del albarán vía la sesión web."""
        url = f"{self.url}/report/pdf/{report_action}/{picking_id}"
        resp = self._web_session.get(url, timeout=60)
        if resp.status_code == 401:
            logger.warning("Sesión expirada, reconectando...")
            self.refresh_session()
            resp = self._web_session.get(url, timeout=60)
        resp.raise_for_status()
        if "application/pdf" not in resp.headers.get("Content-Type", ""):
            raise ValueError(f"Respuesta no es PDF: {resp.headers.get('Content-Type')}")
        return resp.content

    def mark_as_printed(self, picking_id: int) -> bool:
        """Marca el albarán como impreso en Odoo."""
        result = self._models.execute_kw(
            self.db,
            self.uid,
            self.apikey,
            "stock.picking",
            "write",
            [[picking_id], {"x_studio_printed": True}],
        )
        return bool(result)
