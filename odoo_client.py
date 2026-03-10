import xmlrpc.client
import requests
from loguru import logger


class OdooClient:
    def __init__(self, url: str, db: str, user: str, password: str):
        self.url = url.rstrip("/")
        self.db = db
        self.user = user
        self.password = password
        self.uid = None
        self.session_id = None
        self._common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common")
        self._models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object")
        self._web_session = requests.Session()

    def authenticate(self) -> int:
        """Autentica vía XML-RPC y retorna uid."""
        self.uid = self._common.authenticate(self.db, self.user, self.password, {})
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
                    "password": self.password,
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

    def get_confirmed_orders(self, last_check: str) -> list:
        """Busca sale.orders confirmados desde last_check."""
        domain = [
            ["state", "=", "sale"],
            ["write_date", ">=", last_check],
        ]
        fields = ["id", "name", "write_date", "partner_id", "picking_ids"]
        orders = self._models.execute_kw(
            self.db, self.uid, self.password,
            "sale.order", "search_read",
            [domain],
            {"fields": fields, "order": "write_date asc"},
        )
        return orders

    def get_pickings_by_ids(self, picking_ids: list) -> list:
        """Obtiene datos de pickings por sus IDs."""
        if not picking_ids:
            return []
        fields = ["id", "name", "state", "picking_type_code"]
        return self._models.execute_kw(
            self.db, self.uid, self.password,
            "stock.picking", "search_read",
            [[["id", "in", picking_ids]]],
            {"fields": fields},
        )

    def download_pdf(self, record_id: int, report_name: str) -> bytes:
        """Descarga el PDF de un registro vía la sesión web con CSRF token."""
        url = f"{self.url}/report/pdf/{report_name}/{record_id}"
        params = {"csrf_token": self.session_id}
        resp = self._web_session.get(url, params=params, timeout=60)
        if resp.status_code in (401, 403):
            logger.warning("Sesión expirada, reconectando...")
            self.refresh_session()
            params = {"csrf_token": self.session_id}
            resp = self._web_session.get(url, params=params, timeout=60)
        resp.raise_for_status()
        if "application/pdf" not in resp.headers.get("Content-Type", ""):
            raise ValueError(f"Respuesta no es PDF: {resp.headers.get('Content-Type')}")
        return resp.content
