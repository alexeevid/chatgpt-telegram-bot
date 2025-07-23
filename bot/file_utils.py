import io
import pandas as pd
import os
import requests
import logging

YANDEX_DISK_API = "https://cloud-api.yandex.net/v1/disk/resources"

def list_knowledge_base():
    token = os.getenv("YANDEX_TOKEN")
    base_path = os.getenv("YANDEX_KB_PATH", "/База Знаний")

    logging.warning(f"[KB] Токен: {'OK' if token else 'MISSING'} | Путь: {base_path}")

    headers = {"Authorization": f"OAuth {token}"}
    all_files = []

    def list_recursive(path):
        try:
            response = requests.get(
                "https://cloud-api.yandex.net/v1/disk/resources",
                headers=headers,
                params={"path": path, "limit": 1000}
            )
            response.raise_for_status()
            data = response.json()
            items = data.get("_embedded", {}).get("items", [])

            for item in items:
                if item["type"] == "file":
                    all_files.append(item["path"].replace(base_path + "/", ""))
                elif item["type"] == "dir":
                    list_recursive(item["path"])
        except Exception as e:
            logging.exception(f"[KB] Ошибка при обходе {path}: {e}")

    list_recursive(base_path)

    logging.warning(f"[KB] Всего найдено файлов: {len(all_files)}")
    return all_files

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
from PyPDF2 import PdfReader

def extract_text_from_encrypted_pdf(file_path: str, password: str) -> str:
    try:
        with open(file_path, "rb") as file:
            reader = PdfReader(file)
            if reader.is_encrypted:
                reader.decrypt(password)

            text = ""
            for page in reader.pages:
                text += page.extract_text() or ""
            return text.strip()
    except Exception as e:
        return f"⚠️ Ошибка при чтении PDF: {e}"
