import io
import os
import requests
import logging
from typing import Optional
from PyPDF2 import PdfReader

# Глобальный словарь ожидания паролей для пользователей
awaiting_pdf_passwords = {}

YANDEX_DISK_API = "https://cloud-api.yandex.net/v1/disk/resources"

def list_knowledge_base() -> list[str]:
    token = os.getenv("YANDEX_TOKEN")
    base_path = os.getenv("YANDEX_KB_PATH", "disk:/База Знаний")

    logging.info(f"[KB] Используемый путь: {base_path}")

    if not token:
        logging.error("YANDEX_TOKEN отсутствует в переменных окружения")
        return []

    headers = {"Authorization": f"OAuth {token}"}
    all_files = []

    def list_recursive(path):
        try:
            response = requests.get(
                YANDEX_DISK_API,
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
    logging.info(f"[KB] Найдено {len(all_files)} файлов в базе знаний по пути: {base_path}")
    return all_files

def extract_text(fileobj: io.BytesIO, filename: str) -> str:
    filename = filename.lower()

    if filename.endswith('.pdf'):
        try:
            reader = PdfReader(fileobj)
            if reader.is_encrypted:
                return "⚠️ Файл защищён паролем. Пожалуйста, введите пароль."
            return '\n'.join(p.extract_text() or '' for p in reader.pages)
        except Exception as e:
            return f"⚠️ Ошибка при чтении PDF: {e}"

    elif filename.endswith('.txt'):
        return fileobj.read().decode('utf-8', errors='ignore')

    elif filename.endswith('.md'):
        return fileobj.read().decode('utf-8', errors='ignore')

    else:
        raise ValueError(f"Unsupported file format: {filename}")

def extract_text_from_encrypted_pdf(file_path: str, password: str) -> str:
    try:
        with open(file_path, "rb") as file:
            reader = PdfReader(file)
            if reader.is_encrypted:
                result = reader.decrypt(password)
                if result != 1:
                    logging.warning(f"[PDF] Неверный пароль для файла {file_path}")
                    return "⚠️ Неверный пароль или не удалось расшифровать PDF."

            text = ""
            for page in reader.pages:
                text += page.extract_text() or ""
            return text.strip()
    except Exception as e:
        logging.exception(f"[PDF] Ошибка при чтении PDF: {file_path} — {e}")
        return f"⚠️ Ошибка при чтении PDF: {e}"

def set_awaiting_password_file(user_id: int, file_path: str):
    awaiting_pdf_passwords[user_id] = file_path

def get_awaiting_password_file(user_id: int) -> Optional[str]:
    return awaiting_pdf_passwords.get(user_id)

def clear_awaiting_password(user_id: int):
    awaiting_pdf_passwords.pop(user_id, None)
