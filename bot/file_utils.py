from PyPDF2 import PdfReader
from typing import Optional
import os
import logging

# Глобальный словарь ожидания паролей для пользователей
awaiting_pdf_passwords = {}

def list_knowledge_base(root_folder: str) -> list[str]:
    files = []
    for dirpath, _, filenames in os.walk(root_folder):
        for f in filenames:
            if f.lower().endswith(('.pdf', '.txt', '.md')):
                full_path = os.path.join(dirpath, f)
                files.append(full_path)
    return files

def extract_text(file_path: str) -> str:
    try:
        if file_path.lower().endswith(".pdf"):
            with open(file_path, "rb") as file:
                reader = PdfReader(file)
                if reader.is_encrypted:
                    return "⚠️ Файл защищён паролем. Пожалуйста, введите пароль."
                return "\n".join([page.extract_text() or "" for page in reader.pages])
        else:
            with open(file_path, "r", encoding="utf-8") as file:
                return file.read()
    except Exception as e:
        return f"⚠️ Ошибка при чтении файла: {e}"

def extract_text_from_encrypted_pdf(file_path: str, password: str) -> str:
    try:
        with open(file_path, "rb") as file:
            reader = PdfReader(file)
            if reader.is_encrypted:
                result = reader.decrypt(password)
                if result != 1:
                    logging.warning(f"[PDF] Неудачная попытка расшифровки PDF: {file_path}")
                    return "⚠️ Неверный пароль или не удалось расшифровать PDF."

            logging.info(f"[PDF] Успешно расшифрован PDF: {file_path}")
            text = ""
            for page in reader.pages:
                text += page.extract_text() or ""
            return text.strip()
    except Exception as e:
        logging.exception(f"[PDF] Ошибка при чтении PDF: {file_path} — {e}")
        return f"⚠️ Ошибка при чтении PDF: {e}"

def set_awaiting_password(user_id: int, file_path: str):
    awaiting_pdf_passwords[user_id] = file_path

def get_awaiting_password_file(user_id: int) -> Optional[str]:
    return awaiting_pdf_passwords.get(user_id)

def clear_awaiting_password(user_id: int):
    awaiting_pdf_passwords.pop(user_id, None)
