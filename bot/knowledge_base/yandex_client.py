import hashlib
from typing import Iterator, Tuple
import requests
from xml.etree import ElementTree as ET

class YandexDiskClient:
    """Минималистичный WebDAV клиент для Яндекс.Диска."""
    def __init__(self, token: str, base_url: str = "https://webdav.yandex.ru"):
        self.base_url = base_url.rstrip("/")            # убираем хвостовой /
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"OAuth {token}"})

    def _full(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path                           # добавляем ведущий /
        return f"{self.base_url}{path}"

    def iter_files(self, root_path: str) -> Iterator[Tuple[str, int]]:
        url = self._full(root_path)
        resp = self.session.request("PROPFIND", url, headers={"Depth": "infinity"})
        resp.raise_for_status()
        ns = {'d': 'DAV:'}
        root = ET.fromstring(resp.text)
        for r in root.findall('d:response', ns):
            href = r.find('d:href', ns).text
            if href.endswith('/'):
                continue
            size_el = r.find('.//d:getcontentlength', ns)
            size = int(size_el.text) if size_el is not None else 0
            yield href, size

    def download(self, remote_path: str) -> bytes:
        url = self._full(remote_path)
        r = self.session.get(url)
        r.raise_for_status()
        return r.content

    @staticmethod
    def file_signature(content: bytes) -> str:
        return hashlib.md5(content).hexdigest()
