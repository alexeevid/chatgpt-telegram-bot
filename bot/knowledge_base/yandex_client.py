import hashlib
from typing import Iterator, Tuple
import requests
from xml.etree import ElementTree as ET
from urllib.parse import quote

class YandexDiskClient:
    """Минималистичный WebDAV клиент для Яндекс.Диска."""
    def __init__(self, token: str, base_url: str = "https://webdav.yandex.ru"):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"OAuth {token}"})

    def _full(self, path: str) -> str:
        if path.startswith("disk:"):
            path = path[5:]
        if not path.startswith("/"):
            path = "/" + path
        path = quote(path, safe="/")
        return f"{self.base_url}{path}"

    def iter_files(self, root_path: str) -> Iterator[Tuple[str, int]]:
        url = self._full(root_path)
        resp = self.session.request("PROPFIND", url, headers={"Depth": "infinity"})
        if resp.status_code == 401:
            raise RuntimeError(f"401 Unauthorized. Body: {resp.text}")
        resp.raise_for_status()
        ns = {'d': 'DAV:'}
        root = ET.fromstring(resp.text)
        for r in root.findall('d:response', ns):
            href_el = r.find('d:href', ns)
            if href_el is None:
                continue
            href = href_el.text
            if href.endswith('/'):
                continue
            size_el = r.find('.//d:getcontentlength', ns)
            size = int(size_el.text) if size_el is not None else 0
            yield href, size

    def download(self, remote_path: str) -> bytes:
        url = self._full(remote_path)
        r = self.session.get(url)
        if r.status_code == 401:
            raise RuntimeError(f"401 Unauthorized. Body: {r.text}")
        r.raise_for_status()
        return r.content

    @staticmethod
    def file_signature(content: bytes) -> str:
        return hashlib.md5(content).hexdigest()
