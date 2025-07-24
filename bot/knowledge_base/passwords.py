from typing import Dict, Optional

# chat_id -> filename (ожидаем, что пользователь пришлёт пароль к этому файлу)
_awaiting_password: Dict[int, str] = {}

# filename -> password
_pdf_passwords: Dict[str, str] = {}


def set_awaiting_password(chat_id: int, filename: str) -> None:
    _awaiting_password[chat_id] = filename


def get_awaiting_password_file(chat_id: int) -> Optional[str]:
    return _awaiting_password.get(chat_id)


def clear_awaiting_password(chat_id: int) -> None:
    _awaiting_password.pop(chat_id, None)


def store_pdf_password(filename: str, password: str) -> None:
    _pdf_passwords[filename] = password


def get_pdf_password(filename: str) -> Optional[str]:
    return _pdf_passwords.get(filename)
