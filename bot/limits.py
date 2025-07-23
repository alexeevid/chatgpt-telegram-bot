# limits.py

# 📚 Максимум документов из Базы Знаний, добавляемых в контекст
MAX_KB_DOCS = 2

# 📄 Максимальное количество файлов, отображаемых в /kb
MAX_KB_FILES_DISPLAY = 20

# 🧠 Ограничения по GPT
MAX_TOKENS = 2048
TEMPERATURE = 0.7
TOP_P = 1.0

# 📤 Максимальный размер сообщения в Telegram (для чанков)
TELEGRAM_MESSAGE_LIMIT = 4096

# ⌛ Стриминг: таймауты
STREAM_TIMEOUT_GROUP = [180, 120, 90, 50]  # зависит от длины
STREAM_TIMEOUT_PRIVATE = [90, 45, 25, 15]
