from __future__ import annotations

import logging
import os
from typing import Optional, List

import requests
from telegram import (
    Update,
    constants,
    InlineQueryResultArticle,
    InputTextMessageContent,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    ContextTypes,
    filters,
)

from bot.openai_helper import OpenAIHelper, GPT_ALL_MODELS
from bot.usage_tracker import UsageTracker  # можно не использовать, параметр опциональный

# База знаний
from bot.knowledge_base.yandex_client import YandexDiskClient
from bot.knowledge_base.passwords import (
    set_awaiting_password,
    get_awaiting_password_file,
    clear_awaiting_password,
    store_pdf_password,
    get_pdf_password,
)

# Трассер ошибок (если добавлен)
try:
    from bot.error_tracer import capture_exception
except Exception:  # pragma: no cover
    def capture_exception(exc):
        logging.exception(exc)


class ChatGPTTelegramBot:
    def __init__(
        self,
        config: dict,
        openai_helper: OpenAIHelper,
        usage_tracker: Optional[UsageTracker] = None,
        retriever=None,
    ):
        self.config = config
        self.openai = openai_helper
        self.usage_tracker = usage_tracker
        self.retriever = retriever

    # ------------------------------------------------------------------
    # Регистрация хендлеров
    # ------------------------------------------------------------------
    def register_handlers(self, application: Application):
        # 1) Команды
        application.add_handler(CommandHandler("start", self.help))
        application.add_handler(CommandHandler("help", self.help))
        application.add_handler(CommandHandler("reset", self.reset))

        application.add_handler(CommandHandler("kb", self.show_knowledge_base))
        application.add_handler(CommandHandler("pdfpass", self.pdf_pass_command))

        application.add_handler(CommandHandler("list_models", self.list_models))
        application.add_handler(CommandHandler("set_model", self.set_model))

        if self.config.get("enable_image_generation", False):
            application.add_handler(CommandHandler("image", self.image))

        if self.config.get("enable_tts_generation", False):
            application.add_handler(CommandHandler("tts", self.tts))

        application.add_handler(CommandHandler("analyze", self.analyze_command))

        # Callback по KB (если используете inline-кнопки)
        application.add_handler(CallbackQueryHandler(self.handle_kb_selection, pattern=r"^kbselect"))

        # 2) Inline
        application.add_handler(
            InlineQueryHandler(
                self.inline_query,
                chat_types=[
                    constants.ChatType.PRIVATE,
                    constants.ChatType.GROUP,
                    constants.ChatType.SUPERGROUP,
                ],
            )
        )

        # 3) Файлы и медиа
        application.add_handler(MessageHandler(filters.Document.ALL, self.handle_file_upload))
        application.add_handler(MessageHandler(filters.PHOTO, self.handle_photo))
        application.add_handler(MessageHandler(filters.AUDIO | filters.VOICE, self.handle_voice))

        # 4) Пароли PDF — только текст, НЕ команды
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_password_input))

        # 5) Общий текст (LLM-промпт) — в самом конце
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.prompt))

        # 6) Глобальный обработчик ошибок
        application.add_error_handler(self.global_error_handler)

    # ------------------------------------------------------------------
    # Команды
    # ------------------------------------------------------------------
    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text(
            "/start, /help — помощь\n"
            "/reset — сброс диалога\n"
            "/kb [запрос] — показать файлы или поиск в БЗ\n"
            "/pdfpass <file.pdf> <password> — ввести пароль к PDF\n"
            "/list_models — показать доступные модели\n"
            "/set_model <name> — выбрать модель для этого чата\n"
            "/image <prompt> — сгенерировать изображение (если включено)\n"
            "/analyze — проанализировать последний загруженный документ/фото\n"
        )

    async def reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        self.openai.reset_chat_history(chat_id)
        await update.message.reply_text("История диалога сброшена.")

    async def pdf_pass_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Принудительный ввод пароля к конкретному PDF:
        /pdfpass <имя_файла.pdf> <пароль>
        """
        text = (update.message.text or "").strip()
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            await update.message.reply_text("Использование: /pdfpass <имя_файла.pdf> <пароль>")
            return
        filename, password = parts[1], parts[2]
        store_pdf_password(filename, password)
        await update.message.reply_text(f"Пароль для {filename} сохранён.")

    async def list_models(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        allowed: List[str] = self.config.get("allowed_models") or list(GPT_ALL_MODELS)
        await update.message.reply_text(
            "Доступные модели:\n" + "\n".join(f"- {m}" for m in allowed)
        )

    async def set_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        text = (update.message.text or "").strip()
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            await update.message.reply_text("Использование: /set_model <model_name>")
            return
        model = parts[1].strip()
        allowed: List[str] = self.config.get("allowed_models") or list(GPT_ALL_MODELS)
        if model not in allowed:
            await update.message.reply_text("Эта модель не разрешена. Используй /list_models")
            return
        self.openai.user_models[chat_id] = model
        await update.message.reply_text(f"Модель для этого чата установлена: {model}")

    async def image(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.config.get("enable_image_generation", False):
            await update.message.reply_text("Генерация изображений отключена админом.")
            return

        text = (update.message.text or "").strip()
        parts = text.split(" ", 1)
        if len(parts) < 2 or not parts[1].strip():
            await update.message.reply_text("Использование: /image <описание>")
            return

        prompt = parts[1].strip()
        logging.debug("image_command: prompt=%s", prompt)
        try:
            url, size = await self.openai.generate_image(prompt)
            await update.message.reply_photo(url, caption=f"size: {size}")
        except Exception as e:
            capture_exception(e)
            await update.message.reply_text(f"Ошибка генерации изображения: {e}")

    async def tts(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self.config.get("enable_tts_generation", False):
            await update.message.reply_text("TTS отключён админом.")
            return

        text = (update.message.text or "").split(" ", 1)
        if len(text) < 2 or not text[1].strip():
            await update.message.reply_text("Использование: /tts <текст>")
            return

        try:
            audio_bytes, size = await self.openai.generate_speech(text[1].strip())
            await update.message.reply_voice(audio_bytes)
        except Exception as e:
            capture_exception(e)
            await update.message.reply_text(f"Ошибка TTS: {e}")

    async def show_knowledge_base(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logging.warning(">>> Команда /kb вызвана")
        try:
            kb_root = os.getenv("YANDEX_ROOT_PATH", "/knowledge_base")
            if kb_root.startswith("disk:"):
                kb_root = kb_root[5:]
            if not kb_root.startswith("/"):
                kb_root = "/" + kb_root

            base_url_raw = os.getenv("YANDEX_DISK_WEBDAV_URL", "https://webdav.yandex.ru")
            base_url = base_url_raw.rstrip("/")

            token_raw = os.getenv("YANDEX_DISK_TOKEN", "").strip()
            token = token_raw.split(None, 1)[1].strip() if token_raw.lower().startswith("oauth ") else token_raw

            if not token:
                await update.message.reply_text("Не задан YANDEX_DISK_TOKEN")
                return

            logging.debug(
                "YD base_url=%s, root=%s, token_len=%d, token_has_oauth_prefix=%s",
                base_url, kb_root, len(token), token_raw.lower().startswith("oauth ")
            )

            # /kb <query> — поиск
            text = (update.message.text or "")
            query = text.partition(" ")[2].strip()
            if query and getattr(self, "retriever", None):
                try:
                    results = self.retriever.search(query, top_k=5)
                    if not results:
                        await update.message.reply_text("Ничего не найдено.")
                        return
                    reply = "Найдено:\n\n" + "\n\n---\n\n".join(results[:5])
                    await update.message.reply_text(reply[:4000])
                    return
                except Exception as e:
                    capture_exception(e)
                    logging.error("Ошибка поиска в retriever: %s", e, exc_info=True)
                    await update.message.reply_text("Ошибка поиска в базе знаний.")
                    return

            # REST preflight
            try:
                r = requests.get(
                    "https://cloud-api.yandex.net/v1/disk",
                    headers={"Authorization": f"OAuth {token}"},
                    timeout=10,
                )
                if r.status_code == 401:
                    logging.error("REST check failed: 401, body=%s", r.text)
                    await update.message.reply_text(
                        "Токен Я.Диска отвергнут (REST 401). Проверь YANDEX_DISK_TOKEN (без 'OAuth ')."
                    )
                    return
                elif r.status_code >= 400:
                    logging.error("REST check failed: %s, body=%s", r.status_code, r.text)
                    await update.message.reply_text(f"REST check error {r.status_code}: {r.text[:200]}")
                    return
            except requests.exceptions.RequestException as e:
                capture_exception(e)
                logging.error("REST check network error: %s", e, exc_info=True)
                await update.message.reply_text("Не удалось проверить токен через REST API Я.Диска. Проверь сеть.")
                return

            yd = YandexDiskClient(token=token, base_url=base_url)
            files = [path for path, _ in yd.iter_files(kb_root)]

            if not files:
                await update.message.reply_text("В базе знаний нет файлов.")
                return

            reply = "Файлы в базе знаний:\n" + "\n".join(f"- {p}" for p in files[:30])
            if len(files) > 30:
                reply += f"\n… и ещё {len(files) - 30}"

            await update.message.reply_text(reply)

        except requests.exceptions.RequestException as e:
            capture_exception(e)
            logging.error("Сетевой сбой при обращении к Я.Диску: %s", e, exc_info=True)
            await update.message.reply_text("Не удалось подключиться к Яндекс.Диску. Проверь URL/токен/сеть.")
        except Exception as e:
            capture_exception(e)
            logging.error("Ошибка при получении списка файлов из базы знаний", exc_info=True)
            await update.message.reply_text("Не удалось загрузить базу знаний. Проверь токен или путь")

    async def analyze_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        await update.message.reply_text("Команда /analyze не реализована подробно. Загрузите документ/фото — я его разберу.")

    # ------------------------------------------------------------------
    # Контент‑хендлеры
    # ------------------------------------------------------------------
    async def handle_password_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Тихий обработчик ввода пароля к PDF. Не мешает командам.
        """
        text = (update.message.text or "").strip()
        if text.startswith("/"):
            return

        user_id = update.effective_user.id
        file_path = get_awaiting_password_file(user_id)
        if not file_path:
            return

        # TODO: реальная расшифровка PDF
        result = f"(пример) Пароль '{text}' принят для файла {file_path}"
        clear_awaiting_password(user_id)
        await update.message.reply_text(f"🔓 Расшифрованный текст:\n\n{result[:4000]}")

    async def handle_file_upload(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Анализ документов. Замените заглушку на своё извлечение текста.
        """
        try:
            doc = update.message.document
            file = await doc.get_file()
            file_bytes = await file.download_as_bytearray()

            text = f"Документ {doc.file_name} ({doc.file_size} bytes) получен. (Тут сделай извлечение текста)"
            chat_id = update.effective_chat.id
            answer, _ = await self.openai.get_chat_response(chat_id, f"Проанализируй документ:\n{text}")
            await update.message.reply_text(answer[:4000])
        except Exception as e:
            capture_exception(e)
            await update.message.reply_text(f"Ошибка при анализе документа: {e}")

    async def handle_photo(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Анализ фото через vision (interpret_image)"""
        try:
            chat_id = update.effective_chat.id
            photo = update.message.photo[-1]
            file = await photo.get_file()
            file_bytes = await file.download_as_bytearray()
            import io
            bio = io.BytesIO(file_bytes)
            answer, _ = await self.openai.interpret_image(chat_id, bio)
            await update.message.reply_text(answer[:4000])
        except Exception as e:
            capture_exception(e)
            await update.message.reply_text(f"Ошибка анализа изображения: {e}")

    async def handle_voice(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Транскрибация голосовых/аудио."""
        try:
            voice = update.message.voice
            audio = update.message.audio
            file = None
            if voice:
                file = await voice.get_file()
            elif audio:
                file = await audio.get_file()
            else:
                return

            local_path = await file.download_to_drive()
            text = await self.openai.transcribe(str(local_path))
            await update.message.reply_text(f"🗣️ Распознал:\n{text[:4000]}")
        except Exception as e:
            capture_exception(e)
            await update.message.reply_text(f"Ошибка транскрибации: {e}")

    async def inline_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.inline_query.query or ""
        results = [
            InlineQueryResultArticle(
                id="1",
                title="Echo",
                input_message_content=InputTextMessageContent(f"Echo: {query}"),
            )
        ]
        await update.inline_query.answer(results, cache_time=1)

    async def prompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        query = (update.message.text or "").strip()
        try:
            answer, _ = await self.openai.get_chat_response(chat_id, query)
            await update.message.reply_text(answer)
        except Exception as e:
            capture_exception(e)
            await update.message.reply_text(f"Ошибка: {e}")

    # ------------------------------------------------------------------
    # Error handler
    # ------------------------------------------------------------------
    async def global_error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        capture_exception(context.error)
        logging.error("Exception while handling an update:", exc_info=context.error)

    async def post_init(self, application: Application):
        pass
