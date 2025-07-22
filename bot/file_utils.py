import io
import pandas as pd
import os
import requests

YANDEX_DISK_API = 'https://cloud-api.yandex.net/v1/disk/resources'

def list_knowledge_base():
    token = os.getenv("YANDEX_TOKEN")
    path = os.getenv("YANDEX_KB_PATH", "/База Знаний")
    logging.warning(f"[KB] Используем токен: {token[:5]}... и путь: {path}")

    headers = {"Authorization": f"OAuth {token}"}
    params = {"path": path}

    response = requests.get(YANDEX_DISK_API, headers=headers, params=params)
    logging.warning(f"[KB] Ответ от Яндекса: {response.status_code}")
    response.raise_for_status()

    data = response.json()
    items = data.get('_embedded', {}).get('items', [])
    logging.warning(f"[KB] Найдено файлов: {len(items)}")

    return [item['name'] for item in items if item['type'] == 'file']

def extract_text(fileobj: io.BytesIO, filename: str) -> str:
    filename = filename.lower()

    if filename.endswith('.pdf'):
        import pdfplumber
        with pdfplumber.open(fileobj) as pdf:
            return '\n'.join(p.extract_text() or '' for p in pdf.pages)

    elif filename.endswith('.docx'):
        from docx import Document
        document = Document(fileobj)
        return '\n'.join(p.text for p in document.paragraphs)

    elif filename.endswith(('.csv', '.tsv')):
        df = pd.read_csv(fileobj, nrows=50)
        return df.to_markdown()

    elif filename.endswith('.txt'):
        return fileobj.read().decode('utf-8', errors='ignore')

    else:
        raise ValueError(f"Unsupported file format: {filename}")
