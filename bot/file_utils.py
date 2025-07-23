from PyPDF2 import PdfReader
import os

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
                if not reader.decrypt(password):
                    return "⚠️ Неверный пароль или не удалось расшифровать PDF."

            text = ""
            for page in reader.pages:
                text += page.extract_text() or ""
            return text.strip()
    except Exception as e:
        return f"⚠️ Ошибка при чтении PDF: {e}"
