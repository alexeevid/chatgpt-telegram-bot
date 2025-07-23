from __future__ import annotations

import asyncio
import io
import logging
import os
import requests
from datetime import datetime
from uuid import uuid4
from html import escape

from telegram import (
    Update,
    constants,
    BotCommand,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    InlineQueryResultArticle,
    InputTextMessageContent,
    BotCommandScopeAllGroupChats
)
from telegram.ext import (
    ApplicationBuilder,
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    InlineQueryHandler,
    ContextTypes,
    CallbackContext,
    filters
)
from telegram.error import RetryAfter, TimedOut, BadRequest

from bot.file_utils import extract_text, list_knowledge_base
from bot.limits import MAX_KB_DOCS, MAX_KB_FILES_DISPLAY
from bot.openai_helper import OpenAIHelper, localized_text
from bot.usage_tracker import UsageTracker
from bot.db import AsyncSessionLocal
from bot.knowledge_base.yandex_client import YandexDiskClient

from bot.utils import (
    is_group_chat,
    get_thread_id,
    message_text,
    wrap_with_indicator,
    split_into_chunks,
    edit_message_with_retry,
    get_stream_cutoff_values,
    is_allowed,
    get_remaining_budget,
    is_admin,
    is_within_budget,
    get_reply_to_message_id,
    add_chat_request_to_usage_tracker,
    error_handler,
    is_direct_result,
    handle_direct_result,
    cleanup_intermediate_files
)


from PIL import Image
from pydub import AudioSegment

class ChatGPTTelegramBot:
    """
    Class representing a ChatGPT Telegram Bot.
    """

    def __init__(self, config: dict, openai: OpenAIHelper):
        """
        Initializes the bot with the given configuration and GPT bot object.
        :param config: A dictionary containing the bot configuration
        :param openai: OpenAIHelper object
        """
        self.config = config
        self.start_time = datetime.now()
        self.openai = openai
        bot_language = self.config['bot_language']
        self.commands = [
            BotCommand(command='help', description=localized_text('help_description', bot_language)),
            BotCommand(command='reset', description=localized_text('reset_description', bot_language)),
            BotCommand(command='set_model',   description=localized_text('set_model_description',  bot_language)),
            BotCommand(command='list_model',   description=localized_text('list_model_description',  bot_language)),
            BotCommand(command='analyze', description=localized_text('analyze_description', bot_language)),
            #BotCommand(command='stats', description=localized_text('stats_description', bot_language)),
            BotCommand(command='resend', description=localized_text('resend_description', bot_language)),
            #BotCommand(command='balance', description=localized_text('balance_description', bot_language)),
            BotCommand(command='kb', description=localized_text('kb_description', bot_language)),
        ]
        # If imaging is enabled, add the "image" command to the list
        if self.config.get('enable_image_generation', False):
            self.commands.append(BotCommand(command='image', description=localized_text('image_description', bot_language)))

        if self.config.get('enable_tts_generation', False):
            self.commands.append(BotCommand(command='tts', description=localized_text('tts_description', bot_language)))

        self.group_commands = [BotCommand(
            command='chat', description=localized_text('chat_description', bot_language)
        )] + self.commands
        self.disallowed_message = localized_text('disallowed', bot_language)
        self.budget_limit_message = localized_text('budget_limit', bot_language)
        self.usage = {}
        self.last_message = {}
        self.inline_queries_cache = {}
        self.temp_selected_documents = {}
        self.awaiting_password_filter = lambda user_id: user_id in awaiting_pdf_passwords


        # Инициализация выбранных пользователем файлов из Базы Знаний
        self.selected_documents = {}

    from telegram.ext import (
        CommandHandler,
        CallbackQueryHandler,
        InlineQueryHandler,
        MessageHandler,
        filters,
    )

    def register_handlers(self, application):
        # 📌 Команды
        application.add_handler(CommandHandler("start", self.help))
        application.add_handler(CommandHandler("help", self.help))
        application.add_handler(CommandHandler("reset", self.reset))
        application.add_handler(CommandHandler("set_model", self.set_model))
        application.add_handler(CommandHandler("list_model", self.list_models))
        application.add_handler(CommandHandler("analyze", self.analyze))
        application.add_handler(CommandHandler("stats", self.stats))
        application.add_handler(CommandHandler("resend", self.resend))
        application.add_handler(CommandHandler("balance", self.balance))
        application.add_handler(CommandHandler("kb", self.show_knowledge_base))
        application.add_handler(CallbackQueryHandler(self.handle_kb_selection, pattern=r"^kbselect"))
    
        # 🔐 Ввод пароля для защищённых PDF
        application.add_handler(MessageHandler(filters.TEXT & filters.ALL, self.handle_password_input))
    
        # 📄 Загрузка документов
        application.add_handler(MessageHandler(filters.Document.ALL, self.handle_file_upload))
    
        # 🧠 Только если включена генерация изображений
        if self.config.get("enable_image_generation", False):
            application.add_handler(CommandHandler("image", self.image))
    
        # 🔊 Только если включён синтез речи
        if self.config.get("enable_tts_generation", False):
            application.add_handler(CommandHandler("tts", self.tts))
    
        # 🧑‍🤝‍🧑 Команда чата в группах
        application.add_handler(CommandHandler(
            "chat", self.prompt,
            filters=filters.ChatType.GROUP | filters.ChatType.SUPERGROUP
        ))
    
        # 📥 Обработка inline-запросов
        application.add_handler(InlineQueryHandler(
            self.inline_query,
            chat_types=[
                constants.ChatType.PRIVATE,
                constants.ChatType.GROUP,
                constants.ChatType.SUPERGROUP
            ]
        ))
    
        # 🔘 Callback-кнопки
        application.add_handler(CallbackQueryHandler(self.handle_model_selection, pattern=r'^set_model:'))
        application.add_handler(CallbackQueryHandler(self.handle_callback_inline_query, pattern=r'^inline_'))
    
        # 🧾 Текстовые сообщения (не команды)
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.prompt))
    
        # 📄 Документы любого формата
        application.add_handler(MessageHandler(filters.Document.ALL, self.analyze))
    
        # 🖼️ Фото и изображения
        application.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, self.vision))
    
        # 🎙️ Аудио, голосовые и видео
        application.add_handler(MessageHandler(
            filters.AUDIO | filters.VOICE | filters.Document.AUDIO |
            filters.VIDEO | filters.VIDEO_NOTE | filters.Document.VIDEO,
            self.transcribe
        ))
    
        # ⚠️ Обработчик ошибок
        application.add_error_handler(error_handler)

    async def some_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        async with AsyncSessionLocal() as session:
            pass # …работа с session: session.add(...), session.execute(...), await session.commit()
            
    from telegram import Update
    from telegram.ext import ContextTypes

    from utils import get_remaining_budget     # убедитесь, что импорт есть наверху

    #from file_utils import list_knowledge_base
    # Вверху файла (рядом с остальными импортами)

    from file_utils import (
        get_awaiting_password_file,
        clear_awaiting_password,
        extract_text_from_encrypted_pdf
    )
    
    async def handle_password_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        password = update.message.text.strip()
    
        file_path = get_awaiting_password_file(user_id)
        if not file_path:
            return  # Не ожидаем пароль от этого пользователя
    
        text = extract_text_from_encrypted_pdf(file_path, password)
    
        if text.startswith("⚠️"):
            await update.message.reply_text(text)
        else:
            clear_awaiting_password(user_id)
            await update.message.reply_text(f"🔓 Файл расшифрован. Содержимое:\n\n{text[:3000]}")
    
    async def handle_file_upload(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = update.message
        user_id = message.from_user.id
    
        document = message.document
        if not document:
            await message.reply_text("⚠️ Пожалуйста, отправьте файл для анализа.")
            return
    
        file_name = document.file_name
        file = await document.get_file()
        file_data = await file.download_as_bytearray()
    
        file_path = f"/tmp/{file_name}"
        with open(file_path, "wb") as f:
            f.write(file_data)
    
        from file_utils import extract_text, extract_text_from_encrypted_pdf, set_awaiting_password
    
        # Проверка: зашифрован ли PDF
        if file_name.lower().endswith(".pdf"):
            text = extract_text(file_path)
            if "⚠️ Файл защищён паролем" in text:
                set_awaiting_password(user_id, file_path)
                await message.reply_text(f"📄 Файл *{file_name}* защищён паролем.\nПожалуйста, отправьте пароль в чат.", parse_mode=constants.ParseMode.MARKDOWN)
                return
        else:
            text = extract_text(file_path)
    
        if not text.strip():
            await message.reply_text(f"⚠️ Не удалось извлечь текст из файла *{file_name}*.")
            return
    
        # Сохраняем как контекст в чат
        chat_id = update.effective_chat.id
        self.chat_memory[chat_id] = [{"role": "system", "content": f"Context from uploaded file {file_name}:\n{text}"}]
    
        await message.reply_text(f"✅ Контекст из *{file_name}* успешно загружен.", parse_mode=constants.ParseMode.MARKDOWN)
    
    async def show_knowledge_base(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logging.warning(">>> Команда /kb вызвана")
        try:
            kb_root  = os.getenv("YANDEX_ROOT_PATH", "/knowledge_base")
            if not kb_root.startswith("/"):
                kb_root = "/" + kb_root
    
            token    = os.getenv("YANDEX_DISK_TOKEN")
            base_url = os.getenv("YANDEX_DISK_WEBDAV_URL", "https://webdav.yandex.ru").rstrip("/")
    
            if not token:
                await update.message.reply_text("Не задан YANDEX_DISK_TOKEN")
                return
    
            logging.debug("YD base_url=%s, root=%s", base_url, kb_root)
    
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
            logging.error("Сетевой сбой при обращении к Я.Диску: %s", e, exc_info=True)
            await update.message.reply_text("Не удалось подключиться к Яндекс.Диску. Проверь URL/токен/сеть.")
        except Exception as e:
            logging.error("Ошибка при получении списка файлов из базы знаний", exc_info=True)
            await update.message.reply_text("Не удалось загрузить базу знаний. Проверь токен или путь")

    async def handle_password_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Обработка ввода пароля от пользователя, если он ранее открыл PDF с защитой.
        """
        user_id = update.effective_user.id
        message = update.message
    
        file_path = get_awaiting_password_file(user_id)
        if not file_path:
            await message.reply_text("⚠️ Нет ожидающих файлов для ввода пароля.")
            return
    
        password = message.text.strip()
        result = extract_text_from_encrypted_pdf(file_path, password)
    
        if result.startswith("⚠️ Неверный пароль"):
            await message.reply_text(result)
            return
    
        clear_awaiting_password(user_id)
        await message.reply_text(f"🔓 Расшифрованный текст:\n\n{result[:4000]}")
    
    async def handle_kb_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        chat_id = query.message.chat.id

        data = query.data
        if data == "kbselect_done":
            selected = self.temp_selected_documents.get(chat_id, set())
            self.selected_documents[chat_id] = list(selected)
            logging.info(f"[KB] Пользователь {chat_id} выбрал документы: {self.selected_documents[chat_id]}")
            await query.edit_message_text("✅ Выбор документов сохранён.")
            return

        if ":" not in data:
            await query.answer("⚠️ Неверный формат callback.")
            return

        _, short_id = data.split(":")
        filename = self.kb_file_map.get(short_id)
        if not filename:
            await query.answer("⚠️ Файл не найден.")
            return

        selected = self.temp_selected_documents.setdefault(chat_id, set())
        if filename in selected:
            selected.remove(filename)
        else:
            selected.add(filename)

        # 🔁 Перерисовываем клавиатуру с актуальным статусом
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        buttons = []
        for sid, fname in self.kb_file_map.items():
            prefix = "☑️" if fname in selected else "⬜️"
            buttons.append([InlineKeyboardButton(
                f"{prefix} {fname}", callback_data=f"kbselect:{sid}"
            )])

        buttons.append([InlineKeyboardButton("✅ Готово", callback_data="kbselect_done")])

        try:
            await query.edit_message_reply_markup(
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        except Exception as e:
            logging.exception("Ошибка при обновлении кнопок KB")
    
    async def balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        remaining = get_remaining_budget(
            self.config,
            self.usage,
            update,            # ← передаём текущий update
            is_inline=False
        )
        await update.message.reply_text(
            f"💰 Ваш остаток бюджета: ${remaining:.2f}",
            parse_mode=constants.ParseMode.MARKDOWN
        )
        
    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        logging.info("/help triggered")
        message = update.message or (update.callback_query.message if update.callback_query else None)
        if not message:
            logging.warning("No message object in help handler")
            return
    
        bot_language = self.config.get('bot_language', 'en')
    
        help_text = localized_text('help', bot_language)
        if isinstance(help_text, list):
            help_text = help_text[0]  # выбираем нужный язык
    
        await message.reply_text(help_text, parse_mode=constants.ParseMode.MARKDOWN)
        
    async def set_model(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Команда /set_model — выбор модели через кнопки Telegram.
        """
        chat_id = update.effective_chat.id
        # Текущую модель берём либо из пользовательских настроек, либо из конфига OpenAIHelper
        current = self.openai.user_models.get(chat_id, self.openai.config["model"])
    
        # 1) Достаём список моделей из API
        try:
            resp = await self.openai.client.models.list()
            # Отфильтровываем только те, что начинаются на "gpt-"
            available_models = sorted(m.id for m in resp.data if m.id.startswith("gpt-"))
            if not available_models:
                await update.message.reply_text("⚠️ Не найдено доступных GPT-моделей.")
                return
        except OpenAIError as e:
            await update.message.reply_text(f"❌ Ошибка при получении моделей: {e}")
            return
    
        # 2) Строим клавиатуру с кнопками
        keyboard = [
            [InlineKeyboardButton(
                f"✅ {m}" if m == current else m,
                callback_data=f"set_model:{m}"
            )]
            for m in available_models
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
    
        # 3) Отправляем сообщение с кнопками
        await update.message.reply_text(
            f"*Текущая модель:* `{current}`\nВыберите новую:",
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
    
        
    async def handle_model_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
    
        chat_id = query.message.chat.id
        data = query.data  # будет вида "set_model:gpt-4"
    
        if data.startswith("set_model:"):
            selected_model = data.split(":", 1)[1]
            self.openai.user_models[chat_id] = selected_model
            await query.edit_message_text(
                text=f"✅ Модель установлена: *{selected_model}*",
                parse_mode="Markdown"
            )
    
    async def list_models(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Показывает список доступных моделей OpenAI
        """
        bot_language = self.config['bot_language']
        try:
            models = await self.openai.client.models.list()
            model_ids = sorted([m.id for m in models.data if m.id.startswith("gpt-")])

            if not model_ids:
                await update.message.reply_text("Не найдено доступных моделей.")
                return

            message = "*Доступные модели:*\n" + "\n".join(f"• `{m}`" for m in model_ids)
            await update.message.reply_text(message, parse_mode="Markdown")
        except Exception as e:
            logging.exception(e)
            await update.message.reply_text(
                f"Ошибка при получении списка моделей: {str(e)}"
            )

    from telegram import constants
    
    from limits import (
        MAX_KB_DOCS,
        MAX_KB_FILES_DISPLAY,
        MAX_TOKENS,
        TELEGRAM_MESSAGE_LIMIT,
        TEMPERATURE,
        TOP_P,
    )
    
    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        message = update.message or (update.callback_query.message if update.callback_query else None)
        if not message:
            logging.warning("No message object in stats handler")
            return
    
        if not await is_allowed(self.config, update, context):
            logging.warning(f"User {message.from_user.name} (id: {message.from_user.id}) not allowed for /stats")
            await self.send_disallowed_message(update, context)
            return
    
        logging.info(f"User {message.from_user.name} (id: {message.from_user.id}) requested /stats")
    
        user_id = message.from_user.id
        if user_id not in self.usage:
            self.usage[user_id] = UsageTracker(user_id, message.from_user.name)
    
        tokens_today, tokens_month = self.usage[user_id].get_current_token_usage()
        images_today, images_month = self.usage[user_id].get_current_image_count()
        (tr_min_t, tr_sec_t, tr_min_m, tr_sec_m) = self.usage[user_id].get_current_transcription_duration()
        vision_today, vision_month = self.usage[user_id].get_current_vision_tokens()
        chars_today, chars_month = self.usage[user_id].get_current_tts_usage()
        current_cost = self.usage[user_id].get_current_cost()
    
        chat_id = update.effective_chat.id
        chat_messages, chat_token_length = self.openai.get_conversation_stats(chat_id)
        bot_language = self.config['bot_language']
        lt = lambda key: localized_text(key, bot_language)
    
        usage_text = (
            f"*{lt('stats_conversation')[0]}*:\n"
            f"{chat_messages} {lt('stats_conversation')[1]}\n"
            f"{chat_token_length} {lt('stats_conversation')[2]}\n"
            "----------------------------\n"
            f"*{lt('usage_today')}:*\n"
            f"{tokens_today} {lt('stats_tokens')}\n"
        )
    
        if self.config.get('enable_image_generation'):
            usage_text += f"{images_today} {lt('stats_images')}\n"
        if self.config.get('enable_vision'):
            usage_text += f"{vision_today} {lt('stats_vision')}\n"
        if self.config.get('enable_tts_generation'):
            usage_text += f"{chars_today} {lt('stats_tts')}\n"
    
        usage_text += (
            f"{tr_min_t} {lt('stats_transcribe')[0]} {tr_sec_t} {lt('stats_transcribe')[1]}\n"
            f"{lt('stats_total')}{current_cost['cost_today']:.2f}\n"
            "----------------------------\n"
            f"*{lt('usage_month')}:*\n"
            f"{tokens_month} {lt('stats_tokens')}\n"
        )
    
        if self.config.get('enable_image_generation'):
            usage_text += f"{images_month} {lt('stats_images')}\n"
        if self.config.get('enable_vision'):
            usage_text += f"{vision_month} {lt('stats_vision')}\n"
        if self.config.get('enable_tts_generation'):
            usage_text += f"{chars_month} {lt('stats_tts')}\n"
    
        usage_text += (
            f"{tr_min_m} {lt('stats_transcribe')[0]} {tr_sec_m} {lt('stats_transcribe')[1]}\n"
            f"{lt('stats_total')}{current_cost['cost_month']:.2f}"
        )
    
        remaining_budget = get_remaining_budget(self.config, self.usage, update)
        budget_period = self.config.get('budget_period')
        if remaining_budget < float('inf'):
            usage_text += f"\n\n{lt('stats_budget')}{lt(budget_period)}: ${remaining_budget:.2f}."
    
        # 🧮 Добавим секцию с лимитами
        usage_text += (
            f"\n\n*Лимиты и параметры:*\n"
            f"Макс. токенов в ответе: `{self.config.get('max_tokens')}`\n"
            f"Макс. сообщений в истории: `{self.config.get('max_history_size')}`\n"
            f"Макс. возраст истории: `{self.config.get('max_conversation_age_minutes')}` мин\n"
            f"Цена токена: `${self.config.get('token_price')}`\n"
            f"Период бюджета: `{budget_period}`"
        )
    
        await message.reply_text(usage_text, parse_mode=constants.ParseMode.MARKDOWN)
    
    async def resend(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Resend the last request
        """
        if not await is_allowed(self.config, update, context):
            logging.warning(f'User {update.message.from_user.name}  (id: {update.message.from_user.id})'
                            ' is not allowed to resend the message')
            await self.send_disallowed_message(update, context)
            return

        chat_id = update.effective_chat.id
        if chat_id not in self.last_message:
            logging.warning(f'User {update.message.from_user.name} (id: {update.message.from_user.id})'
                            ' does not have anything to resend')
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                text=localized_text('resend_failed', self.config['bot_language'])
            )
            return

        # Update message text, clear self.last_message and send the request to prompt
        logging.info(f'Resending the last prompt from user: {update.message.from_user.name} '
                     f'(id: {update.message.from_user.id})')
        with update.message._unfrozen() as message:
            message.text = self.last_message.pop(chat_id)

        await self.prompt(update=update, context=context)

    async def reset(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Resets the conversation and clears selected knowledge base files.
        """
        if not await is_allowed(self.config, update, context):
            logging.warning(f'User {update.message.from_user.name} (id: {update.message.from_user.id}) '
                            'is not allowed to reset the conversation')
            await self.send_disallowed_message(update, context)
            return
    
        logging.info(f'Resetting the conversation for user {update.message.from_user.name} '
                     f'(id: {update.message.from_user.id})...')
    
        chat_id = update.effective_chat.id
    
        # Сброс истории OpenAI
        reset_content = message_text(update.message)
        self.openai.reset_chat_history(chat_id=chat_id, content=reset_content)
    
        # Сброс выбранных файлов из базы знаний
        if hasattr(self, "selected_documents"):
            self.selected_documents[chat_id] = []
        else:
            self.selected_documents = {chat_id: []}
    
        await update.effective_message.reply_text(
            message_thread_id=get_thread_id(update),
            text=localized_text('reset_done', self.config['bot_language'])
        )

    import io  # убедись, что импорт есть вверху

    async def analyze(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Команда /analyze — анализ документа. Работает с PDF, DOCX, TXT, CSV.
        """
        if not await self.check_allowed_and_within_budget(update, context):
            return
    
        if not update.message.document:
            await update.message.reply_text("Пожалуйста, прикрепите файл для анализа (PDF, DOCX, CSV или TXT).")
            return
    
        user_id = update.message.from_user.id
        doc = update.message.document
    
        try:
            telegram_file = await context.bot.get_file(doc.file_id)
            file_buffer = io.BytesIO(await telegram_file.download_as_bytearray())
            raw_text = extract_text(file_buffer, doc.file_name)
    
            if not raw_text.strip():
                await update.message.reply_text("Не удалось извлечь текст из файла.")
                return
    
            prompt_text = raw_text[:4000]  # ограничим объём
            system_msg = "You are a professional analyst. Summarize the key points, risks, and recommendations."
            user_msg = f"Проанализируй следующий текст:\n\n{prompt_text}"
    
            response = await self.openai.client.chat.completions.create(
                model=self.openai.config["model"],
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg}
                ]
            )
    
            answer = response.choices[0].message.content
            await update.message.reply_text(answer[:4000])
    
            if hasattr(response, "usage") and response.usage:
                add_chat_request_to_usage_tracker(
                    self.usage, self.config, user_id, response.usage.total_tokens
                )
    
        except Exception as e:
            logging.exception(e)
            await update.message.reply_text(f"Ошибка анализа: {e}")
    
    async def image(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Generates an image for the given prompt using DALL·E APIs
        """
        if not self.config['enable_image_generation'] \
                or not await self.check_allowed_and_within_budget(update, context):
            return

        image_query = message_text(update.message)
        if image_query == '':
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                text=localized_text('image_no_prompt', self.config['bot_language'])
            )
            return

        logging.info(f'New image generation request received from user {update.message.from_user.name} '
                     f'(id: {update.message.from_user.id})')

        async def _generate():
            try:
                image_url, image_size = await self.openai.generate_image(prompt=image_query)
                logging.info(f"Image generated successfully: {image_url} | Size: {image_size}")
                if self.config['image_receive_mode'] == 'photo':
                    await update.effective_message.reply_photo(
                        reply_to_message_id=get_reply_to_message_id(self.config, update),
                        photo=image_url
                    )
                elif self.config['image_receive_mode'] == 'document':
                    await update.effective_message.reply_document(
                        reply_to_message_id=get_reply_to_message_id(self.config, update),
                        document=image_url
                    )
                else:
                    raise Exception(f"env variable IMAGE_RECEIVE_MODE has invalid value {self.config['image_receive_mode']}")
                # add image request to users usage tracker
                user_id = update.message.from_user.id
                self.usage[user_id].add_image_request(image_size, self.config['image_prices'])
                # add guest chat request to guest usage tracker
                if str(user_id) not in self.config['allowed_user_ids'].split(',') and 'guests' in self.usage:
                    self.usage["guests"].add_image_request(image_size, self.config['image_prices'])

            except Exception as e:
                logging.exception(e)
                await update.effective_message.reply_text(
                    message_thread_id=get_thread_id(update),
                    reply_to_message_id=get_reply_to_message_id(self.config, update),
                    text=f"{localized_text('image_fail', self.config['bot_language'])}: {str(e)}",
                    parse_mode=constants.ParseMode.MARKDOWN
                )

        await wrap_with_indicator(update, context, _generate, constants.ChatAction.UPLOAD_PHOTO)

    async def tts(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Generates an speech for the given input using TTS APIs
        """
        if not self.config['enable_tts_generation'] \
                or not await self.check_allowed_and_within_budget(update, context):
            return

        tts_query = message_text(update.message)
        if tts_query == '':
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                text=localized_text('tts_no_prompt', self.config['bot_language'])
            )
            return

        logging.info(f'New speech generation request received from user {update.message.from_user.name} '
                     f'(id: {update.message.from_user.id})')

        async def _generate():
            try:
                speech_file, text_length = await self.openai.generate_speech(text=tts_query)

                await update.effective_message.reply_voice(
                    reply_to_message_id=get_reply_to_message_id(self.config, update),
                    voice=speech_file
                )
                speech_file.close()
                # add image request to users usage tracker
                user_id = update.message.from_user.id
                self.usage[user_id].add_tts_request(text_length, self.config['tts_model'], self.config['tts_prices'])
                # add guest chat request to guest usage tracker
                if str(user_id) not in self.config['allowed_user_ids'].split(',') and 'guests' in self.usage:
                    self.usage["guests"].add_tts_request(text_length, self.config['tts_model'], self.config['tts_prices'])

            except Exception as e:
                logging.exception(e)
                await update.effective_message.reply_text(
                    message_thread_id=get_thread_id(update),
                    reply_to_message_id=get_reply_to_message_id(self.config, update),
                    text=f"{localized_text('tts_fail', self.config['bot_language'])}: {str(e)}",
                    parse_mode=constants.ParseMode.MARKDOWN
                )

        await wrap_with_indicator(update, context, _generate, constants.ChatAction.UPLOAD_VOICE)

    async def transcribe(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Transcribe audio messages.
        """
        if not self.config['enable_transcription'] or not await self.check_allowed_and_within_budget(update, context):
            return

        if is_group_chat(update) and self.config['ignore_group_transcriptions']:
            logging.info('Transcription coming from group chat, ignoring...')
            return

        chat_id = update.effective_chat.id
        filename = update.message.effective_attachment.file_unique_id

        async def _execute():
            filename_mp3 = f'{filename}.mp3'
            bot_language = self.config['bot_language']
            try:
                media_file = await context.bot.get_file(update.message.effective_attachment.file_id)
                await media_file.download_to_drive(filename)
            except Exception as e:
                logging.exception(e)
                await update.effective_message.reply_text(
                    message_thread_id=get_thread_id(update),
                    reply_to_message_id=get_reply_to_message_id(self.config, update),
                    text=(
                        f"{localized_text('media_download_fail', bot_language)[0]}: "
                        f"{str(e)}. {localized_text('media_download_fail', bot_language)[1]}"
                    ),
                    parse_mode=constants.ParseMode.MARKDOWN
                )
                return

            try:
                audio_track = AudioSegment.from_file(filename)
                audio_track.export(filename_mp3, format="mp3")
                logging.info(f'New transcribe request received from user {update.message.from_user.name} '
                             f'(id: {update.message.from_user.id})')

            except Exception as e:
                logging.exception(e)
                await update.effective_message.reply_text(
                    message_thread_id=get_thread_id(update),
                    reply_to_message_id=get_reply_to_message_id(self.config, update),
                    text=localized_text('media_type_fail', bot_language)
                )
                if os.path.exists(filename):
                    os.remove(filename)
                return

            user_id = update.message.from_user.id
            if user_id not in self.usage:
                self.usage[user_id] = UsageTracker(user_id, update.message.from_user.name)

            try:
                transcript = await self.openai.transcribe(filename_mp3)

                transcription_price = self.config['transcription_price']
                self.usage[user_id].add_transcription_seconds(audio_track.duration_seconds, transcription_price)

                allowed_user_ids = self.config['allowed_user_ids'].split(',')
                if str(user_id) not in allowed_user_ids and 'guests' in self.usage:
                    self.usage["guests"].add_transcription_seconds(audio_track.duration_seconds, transcription_price)

                # check if transcript starts with any of the prefixes
                response_to_transcription = any(transcript.lower().startswith(prefix.lower()) if prefix else False
                                                for prefix in self.config['voice_reply_prompts'])

                if self.config['voice_reply_transcript'] and not response_to_transcription:

                    # Split into chunks of 4096 characters (Telegram's message limit)
                    transcript_output = f"_{localized_text('transcript', bot_language)}:_\n\"{transcript}\""
                    chunks = split_into_chunks(transcript_output)

                    for index, transcript_chunk in enumerate(chunks):
                        await update.effective_message.reply_text(
                            message_thread_id=get_thread_id(update),
                            reply_to_message_id=get_reply_to_message_id(self.config, update) if index == 0 else None,
                            text=transcript_chunk,
                            parse_mode=constants.ParseMode.MARKDOWN
                        )
                else:
                    # Get the response of the transcript
                    response, total_tokens = await self.openai.get_chat_response(chat_id=chat_id, query=transcript)

                    self.usage[user_id].add_chat_tokens(total_tokens, self.config['token_price'])
                    if str(user_id) not in allowed_user_ids and 'guests' in self.usage:
                        self.usage["guests"].add_chat_tokens(total_tokens, self.config['token_price'])

                    # Split into chunks of 4096 characters (Telegram's message limit)
                    transcript_output = (
                        f"_{localized_text('transcript', bot_language)}:_\n\"{transcript}\"\n\n"
                        f"_{localized_text('answer', bot_language)}:_\n{response}"
                    )
                    chunks = split_into_chunks(transcript_output)

                    for index, transcript_chunk in enumerate(chunks):
                        await update.effective_message.reply_text(
                            message_thread_id=get_thread_id(update),
                            reply_to_message_id=get_reply_to_message_id(self.config, update) if index == 0 else None,
                            text=transcript_chunk,
                            parse_mode=constants.ParseMode.MARKDOWN
                        )

            except Exception as e:
                logging.exception(e)
                await update.effective_message.reply_text(
                    message_thread_id=get_thread_id(update),
                    reply_to_message_id=get_reply_to_message_id(self.config, update),
                    text=f"{localized_text('transcribe_fail', bot_language)}: {str(e)}",
                    parse_mode=constants.ParseMode.MARKDOWN
                )
            finally:
                if os.path.exists(filename_mp3):
                    os.remove(filename_mp3)
                if os.path.exists(filename):
                    os.remove(filename)

        await wrap_with_indicator(update, context, _execute, constants.ChatAction.TYPING)

    async def vision(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        Interpret image using vision model.
        """
        if not self.config['enable_vision'] or not await self.check_allowed_and_within_budget(update, context):
            return

        chat_id = update.effective_chat.id
        prompt = update.message.caption

        if is_group_chat(update):
            if self.config['ignore_group_vision']:
                logging.info('Vision coming from group chat, ignoring...')
                return
            else:
                trigger_keyword = self.config['group_trigger_keyword']
                if (prompt is None and trigger_keyword != '') or \
                   (prompt is not None and not prompt.lower().startswith(trigger_keyword.lower())):
                    logging.info('Vision coming from group chat with wrong keyword, ignoring...')
                    return
        
        image = update.message.effective_attachment[-1]
        

        async def _execute():
            bot_language = self.config['bot_language']
            try:
                media_file = await context.bot.get_file(image.file_id)
                temp_file = io.BytesIO(await media_file.download_as_bytearray())
            except Exception as e:
                logging.exception(e)
                await update.effective_message.reply_text(
                    message_thread_id=get_thread_id(update),
                    reply_to_message_id=get_reply_to_message_id(self.config, update),
                    text=(
                        f"{localized_text('media_download_fail', bot_language)[0]}: "
                        f"{str(e)}. {localized_text('media_download_fail', bot_language)[1]}"
                    ),
                    parse_mode=constants.ParseMode.MARKDOWN
                )
                return
            
            # convert jpg from telegram to png as understood by openai

            temp_file_png = io.BytesIO()

            try:
                original_image = Image.open(temp_file)
                
                original_image.save(temp_file_png, format='PNG')
                logging.info(f'New vision request received from user {update.message.from_user.name} '
                             f'(id: {update.message.from_user.id})')

            except Exception as e:
                logging.exception(e)
                await update.effective_message.reply_text(
                    message_thread_id=get_thread_id(update),
                    reply_to_message_id=get_reply_to_message_id(self.config, update),
                    text=localized_text('media_type_fail', bot_language)
                )
            
            

            user_id = update.message.from_user.id
            if user_id not in self.usage:
                self.usage[user_id] = UsageTracker(user_id, update.message.from_user.name)

            if self.config['stream']:

                stream_response = self.openai.interpret_image_stream(chat_id=chat_id, fileobj=temp_file_png, prompt=prompt)
                i = 0
                prev = ''
                sent_message = None
                backoff = 0
                stream_chunk = 0

                async for content, tokens in stream_response:
                    if is_direct_result(content):
                        return await handle_direct_result(self.config, update, content)

                    if len(content.strip()) == 0:
                        continue

                    stream_chunks = split_into_chunks(content)
                    if len(stream_chunks) > 1:
                        content = stream_chunks[-1]
                        if stream_chunk != len(stream_chunks) - 1:
                            stream_chunk += 1
                            try:
                                await edit_message_with_retry(context, chat_id, str(sent_message.message_id),
                                                              stream_chunks[-2])
                            except:
                                pass
                            try:
                                sent_message = await update.effective_message.reply_text(
                                    message_thread_id=get_thread_id(update),
                                    text=content if len(content) > 0 else "..."
                                )
                            except:
                                pass
                            continue

                    cutoff = get_stream_cutoff_values(update, content)
                    cutoff += backoff

                    if i == 0:
                        try:
                            if sent_message is not None:
                                await context.bot.delete_message(chat_id=sent_message.chat_id,
                                                                 message_id=sent_message.message_id)
                            sent_message = await update.effective_message.reply_text(
                                message_thread_id=get_thread_id(update),
                                reply_to_message_id=get_reply_to_message_id(self.config, update),
                                text=content,
                            )
                        except:
                            continue

                    elif abs(len(content) - len(prev)) > cutoff or tokens != 'not_finished':
                        prev = content

                        try:
                            use_markdown = tokens != 'not_finished'
                            await edit_message_with_retry(context, chat_id, str(sent_message.message_id),
                                                          text=content, markdown=use_markdown)

                        except RetryAfter as e:
                            backoff += 5
                            await asyncio.sleep(e.retry_after)
                            continue

                        except TimedOut:
                            backoff += 5
                            await asyncio.sleep(0.5)
                            continue

                        except Exception:
                            backoff += 5
                            continue

                        await asyncio.sleep(0.01)

                    i += 1
                    if tokens != 'not_finished':
                        total_tokens = int(tokens)

                
            else:

                try:
                    interpretation, total_tokens = await self.openai.interpret_image(chat_id, temp_file_png, prompt=prompt)


                    try:
                        await update.effective_message.reply_text(
                            message_thread_id=get_thread_id(update),
                            reply_to_message_id=get_reply_to_message_id(self.config, update),
                            text=interpretation,
                            parse_mode=constants.ParseMode.MARKDOWN
                        )
                    except BadRequest:
                        try:
                            await update.effective_message.reply_text(
                                message_thread_id=get_thread_id(update),
                                reply_to_message_id=get_reply_to_message_id(self.config, update),
                                text=interpretation
                            )
                        except Exception as e:
                            logging.exception(e)
                            await update.effective_message.reply_text(
                                message_thread_id=get_thread_id(update),
                                reply_to_message_id=get_reply_to_message_id(self.config, update),
                                text=f"{localized_text('vision_fail', bot_language)}: {str(e)}",
                                parse_mode=constants.ParseMode.MARKDOWN
                            )
                except Exception as e:
                    logging.exception(e)
                    await update.effective_message.reply_text(
                        message_thread_id=get_thread_id(update),
                        reply_to_message_id=get_reply_to_message_id(self.config, update),
                        text=f"{localized_text('vision_fail', bot_language)}: {str(e)}",
                        parse_mode=constants.ParseMode.MARKDOWN
                    )
            vision_token_price = self.config['vision_token_price']
            self.usage[user_id].add_vision_tokens(total_tokens, vision_token_price)

            allowed_user_ids = self.config['allowed_user_ids'].split(',')
            if str(user_id) not in allowed_user_ids and 'guests' in self.usage:
                self.usage["guests"].add_vision_tokens(total_tokens, vision_token_price)

        await wrap_with_indicator(update, context, _execute, constants.ChatAction.TYPING)

    async def prompt(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """
        React to incoming messages and respond accordingly.
        """
        await add_chat_request_to_usage_tracker(self.config, self.usage, update)
        if update.edited_message or not update.message or update.message.via_bot:
            return

        if not await self.check_allowed_and_within_budget(update, context):
            return

        logging.info(
            f'New message received from user {update.message.from_user.name} (id: {update.message.from_user.id})')
        chat_id = update.effective_chat.id
        user_id = update.message.from_user.id
        prompt = message_text(update.message)
        # 📚 Добавим тексты выбранных документов из базы знаний (если есть)
        context_parts = []
        selected = self.selected_documents.get(chat_id, [])
        max_docs = MAX_KB_DOCS  # ограничим количеством документов

        for i, doc in enumerate(selected[:max_docs]):
            try:
                content = await self.load_document_content(doc)
                context_parts.append(f"[Документ {i+1}: {doc}]\n{content.strip()[:3000]}")
            except Exception as e:
                logging.warning(f"Не удалось загрузить {doc}: {e}")

        if context_parts:
            context_text = "\n\n".join(context_parts)
            prompt = f"📚 Контекст из базы знаний:\n{context_text}\n\n🔎 Вопрос:\n{prompt}"
        self.last_message[chat_id] = prompt

        if is_group_chat(update):
            trigger_keyword = self.config['group_trigger_keyword']

            if prompt.lower().startswith(trigger_keyword.lower()) or update.message.text.lower().startswith('/chat'):
                if prompt.lower().startswith(trigger_keyword.lower()):
                    prompt = prompt[len(trigger_keyword):].strip()

                if update.message.reply_to_message and \
                        update.message.reply_to_message.text and \
                        update.message.reply_to_message.from_user.id != context.bot.id:
                    prompt = f'"{update.message.reply_to_message.text}" {prompt}'
            else:
                if update.message.reply_to_message and update.message.reply_to_message.from_user.id == context.bot.id:
                    logging.info('Message is a reply to the bot, allowing...')
                else:
                    logging.warning('Message does not start with trigger keyword, ignoring...')
                    return

        try:
            total_tokens = 0

            if self.config['stream']:
                await update.effective_message.reply_chat_action(
                    action=constants.ChatAction.TYPING,
                    message_thread_id=get_thread_id(update)
                )

                stream_response = self.openai.get_chat_response_stream(chat_id=chat_id, query=prompt)
                i = 0
                prev = ''
                sent_message = None
                backoff = 0
                stream_chunk = 0

                async for content, tokens in stream_response:
                    if is_direct_result(content):
                        return await handle_direct_result(self.config, update, content)

                    if len(content.strip()) == 0:
                        continue

                    stream_chunks = split_into_chunks(content)
                    if len(stream_chunks) > 1:
                        content = stream_chunks[-1]
                        if stream_chunk != len(stream_chunks) - 1:
                            stream_chunk += 1
                            try:
                                await edit_message_with_retry(context, chat_id, str(sent_message.message_id),
                                                              stream_chunks[-2])
                            except:
                                pass
                            try:
                                sent_message = await update.effective_message.reply_text(
                                    message_thread_id=get_thread_id(update),
                                    text=content if len(content) > 0 else "..."
                                )
                            except:
                                pass
                            continue

                    cutoff = get_stream_cutoff_values(update, content)
                    cutoff += backoff

                    if i == 0:
                        try:
                            if sent_message is not None:
                                await context.bot.delete_message(chat_id=sent_message.chat_id,
                                                                 message_id=sent_message.message_id)
                            sent_message = await update.effective_message.reply_text(
                                message_thread_id=get_thread_id(update),
                                reply_to_message_id=get_reply_to_message_id(self.config, update),
                                text=content,
                            )
                        except:
                            continue

                    elif abs(len(content) - len(prev)) > cutoff or tokens != 'not_finished':
                        prev = content

                        try:
                            use_markdown = tokens != 'not_finished'
                            await edit_message_with_retry(context, chat_id, str(sent_message.message_id),
                                                          text=content, markdown=use_markdown)

                        except RetryAfter as e:
                            backoff += 5
                            await asyncio.sleep(e.retry_after)
                            continue

                        except TimedOut:
                            backoff += 5
                            await asyncio.sleep(0.5)
                            continue

                        except Exception:
                            backoff += 5
                            continue

                        await asyncio.sleep(0.01)

                    i += 1
                    if tokens != 'not_finished':
                        total_tokens = int(tokens)

            else:
                async def _reply():
                    nonlocal total_tokens
                    response, total_tokens = await self.openai.get_chat_response(chat_id=chat_id, query=prompt)

                    if is_direct_result(response):
                        return await handle_direct_result(self.config, update, response)

                    # Split into chunks of 4096 characters (Telegram's message limit)
                    chunks = split_into_chunks(response)

                    for index, chunk in enumerate(chunks):
                        try:
                            await update.effective_message.reply_text(
                                message_thread_id=get_thread_id(update),
                                reply_to_message_id=get_reply_to_message_id(self.config,
                                                                            update) if index == 0 else None,
                                text=chunk,
                                parse_mode=constants.ParseMode.MARKDOWN
                            )
                        except Exception:
                            try:
                                await update.effective_message.reply_text(
                                    message_thread_id=get_thread_id(update),
                                    reply_to_message_id=get_reply_to_message_id(self.config,
                                                                                update) if index == 0 else None,
                                    text=chunk
                                )
                            except Exception as exception:
                                raise exception

                await wrap_with_indicator(update, context, _reply, constants.ChatAction.TYPING)

            add_chat_request_to_usage_tracker(self.usage, self.config, user_id, total_tokens)

        except Exception as e:
            logging.exception(e)
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                reply_to_message_id=get_reply_to_message_id(self.config, update),
                text=f"{localized_text('chat_fail', self.config['bot_language'])} {str(e)}",
                parse_mode=constants.ParseMode.MARKDOWN
            )
    
    async def load_document_content(self, doc_name: str) -> str:
        """
        Загружает содержимое документа из Яндекс.Диска.
        """
        import requests

        token = os.getenv("YANDEX_TOKEN")
        path = os.getenv("YANDEX_KB_PATH", "/База Знаний") + "/" + doc_name
        headers = {"Authorization": f"OAuth {token}"}

        # Получаем временную ссылку
        meta = requests.get(
            "https://cloud-api.yandex.net/v1/disk/resources/download",
            headers=headers,
            params={"path": path}
        )
        meta.raise_for_status()
        href = meta.json()["href"]

        # Скачиваем файл
        file_response = requests.get(href)
        file_response.raise_for_status()

        from io import BytesIO
        from file_utils import extract_text

        return extract_text(BytesIO(file_response.content), doc_name)
    
    async def inline_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """
        Handle the inline query. This is run when you type: @botusername <query>
        """
        query = update.inline_query.query
        if len(query) < 3:
            return
        if not await self.check_allowed_and_within_budget(update, context, is_inline=True):
            return

        callback_data_suffix = "gpt:"
        result_id = str(uuid4())
        self.inline_queries_cache[result_id] = query
        callback_data = f'{callback_data_suffix}{result_id}'

        await self.send_inline_query_result(update, result_id, message_content=query, callback_data=callback_data)

    async def send_inline_query_result(self, update: Update, result_id, message_content, callback_data=""):
        """
        Send inline query result
        """
        try:
            reply_markup = None
            bot_language = self.config['bot_language']
            if callback_data:
                reply_markup = InlineKeyboardMarkup([[
                    InlineKeyboardButton(text=f'🤖 {localized_text("answer_with_chatgpt", bot_language)}',
                                         callback_data=callback_data)
                ]])

            inline_query_result = InlineQueryResultArticle(
                id=result_id,
                title=localized_text("ask_chatgpt", bot_language),
                input_message_content=InputTextMessageContent(message_content),
                description=message_content,
                thumbnail_url='https://user-images.githubusercontent.com/11541888/223106202-7576ff11-2c8e-408d-94ea-b02a7a32149a.png',
                reply_markup=reply_markup
            )

            await update.inline_query.answer([inline_query_result], cache_time=0)
        except Exception as e:
            logging.error(f'An error occurred while generating the result card for inline query {e}')

    async def handle_callback_inline_query(self, update: Update, context: CallbackContext):
        """
        Handle the callback query from the inline query result
        """
        callback_data = update.callback_query.data
        user_id = update.callback_query.from_user.id
        inline_message_id = update.callback_query.inline_message_id
        name = update.callback_query.from_user.name
        callback_data_suffix = "gpt:"
        query = ""
        bot_language = self.config['bot_language']
        answer_tr = localized_text("answer", bot_language)
        loading_tr = localized_text("loading", bot_language)

        try:
            if callback_data.startswith(callback_data_suffix):
                unique_id = callback_data.split(':')[1]
                total_tokens = 0

                # Retrieve the prompt from the cache
                query = self.inline_queries_cache.get(unique_id)
                if query:
                    self.inline_queries_cache.pop(unique_id)
                else:
                    error_message = (
                        f'{localized_text("error", bot_language)}. '
                        f'{localized_text("try_again", bot_language)}'
                    )
                    await edit_message_with_retry(context, chat_id=None, message_id=inline_message_id,
                                                  text=f'{query}\n\n_{answer_tr}:_\n{error_message}',
                                                  is_inline=True)
                    return

                unavailable_message = localized_text("function_unavailable_in_inline_mode", bot_language)
                if self.config['stream']:
                    stream_response = self.openai.get_chat_response_stream(chat_id=user_id, query=query)
                    i = 0
                    prev = ''
                    backoff = 0
                    async for content, tokens in stream_response:
                        if is_direct_result(content):
                            cleanup_intermediate_files(content)
                            await edit_message_with_retry(context, chat_id=None,
                                                          message_id=inline_message_id,
                                                          text=f'{query}\n\n_{answer_tr}:_\n{unavailable_message}',
                                                          is_inline=True)
                            return

                        if len(content.strip()) == 0:
                            continue

                        cutoff = get_stream_cutoff_values(update, content)
                        cutoff += backoff

                        if i == 0:
                            try:
                                await edit_message_with_retry(context, chat_id=None,
                                                              message_id=inline_message_id,
                                                              text=f'{query}\n\n{answer_tr}:\n{content}',
                                                              is_inline=True)
                            except:
                                continue

                        elif abs(len(content) - len(prev)) > cutoff or tokens != 'not_finished':
                            prev = content
                            try:
                                use_markdown = tokens != 'not_finished'
                                divider = '_' if use_markdown else ''
                                text = f'{query}\n\n{divider}{answer_tr}:{divider}\n{content}'

                                # We only want to send the first 4096 characters. No chunking allowed in inline mode.
                                text = text[:4096]

                                await edit_message_with_retry(context, chat_id=None, message_id=inline_message_id,
                                                              text=text, markdown=use_markdown, is_inline=True)

                            except RetryAfter as e:
                                backoff += 5
                                await asyncio.sleep(e.retry_after)
                                continue
                            except TimedOut:
                                backoff += 5
                                await asyncio.sleep(0.5)
                                continue
                            except Exception:
                                backoff += 5
                                continue

                            await asyncio.sleep(0.01)

                        i += 1
                        if tokens != 'not_finished':
                            total_tokens = int(tokens)

                else:
                    async def _send_inline_query_response():
                        nonlocal total_tokens
                        # Edit the current message to indicate that the answer is being processed
                        await context.bot.edit_message_text(inline_message_id=inline_message_id,
                                                            text=f'{query}\n\n_{answer_tr}:_\n{loading_tr}',
                                                            parse_mode=constants.ParseMode.MARKDOWN)

                        logging.info(f'Generating response for inline query by {name}')
                        response, total_tokens = await self.openai.get_chat_response(chat_id=user_id, query=query)

                        if is_direct_result(response):
                            cleanup_intermediate_files(response)
                            await edit_message_with_retry(context, chat_id=None,
                                                          message_id=inline_message_id,
                                                          text=f'{query}\n\n_{answer_tr}:_\n{unavailable_message}',
                                                          is_inline=True)
                            return

                        text_content = f'{query}\n\n_{answer_tr}:_\n{response}'

                        # We only want to send the first 4096 characters. No chunking allowed in inline mode.
                        text_content = text_content[:4096]

                        # Edit the original message with the generated content
                        await edit_message_with_retry(context, chat_id=None, message_id=inline_message_id,
                                                      text=text_content, is_inline=True)

                    await wrap_with_indicator(update, context, _send_inline_query_response,
                                              constants.ChatAction.TYPING, is_inline=True)

                add_chat_request_to_usage_tracker(self.usage, self.config, user_id, total_tokens)

        except Exception as e:
            logging.error(f'Failed to respond to an inline query via button callback: {e}')
            logging.exception(e)
            localized_answer = localized_text('chat_fail', self.config['bot_language'])
            await edit_message_with_retry(context, chat_id=None, message_id=inline_message_id,
                                          text=f"{query}\n\n_{answer_tr}:_\n{localized_answer} {str(e)}",
                                          is_inline=True)

    async def check_allowed_and_within_budget(self, update: Update, context: ContextTypes.DEFAULT_TYPE,
                                              is_inline=False) -> bool:
        """
        Checks if the user is allowed to use the bot and if they are within their budget
        :param update: Telegram update object
        :param context: Telegram context object
        :param is_inline: Boolean flag for inline queries
        :return: Boolean indicating if the user is allowed to use the bot
        """
        name = update.inline_query.from_user.name if is_inline else update.message.from_user.name
        user_id = update.inline_query.from_user.id if is_inline else update.message.from_user.id

        if not await is_allowed(self.config, update, context, is_inline=is_inline):
            logging.warning(f'User {name} (id: {user_id}) is not allowed to use the bot')
            await self.send_disallowed_message(update, context, is_inline)
            return False
        if not is_within_budget(self.config, self.usage, update, is_inline=is_inline):
            logging.warning(f'User {name} (id: {user_id}) reached their usage limit')
            await self.send_budget_reached_message(update, context, is_inline)
            return False

        return True

    async def send_disallowed_message(self, update: Update, _: ContextTypes.DEFAULT_TYPE, is_inline=False):
        """
        Sends the disallowed message to the user.
        """
        if not is_inline:
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                text=self.disallowed_message,
                disable_web_page_preview=True
            )
        else:
            result_id = str(uuid4())
            await self.send_inline_query_result(update, result_id, message_content=self.disallowed_message)

    async def send_budget_reached_message(self, update: Update, _: ContextTypes.DEFAULT_TYPE, is_inline=False):
        """
        Sends the budget reached message to the user.
        """
        if not is_inline:
            await update.effective_message.reply_text(
                message_thread_id=get_thread_id(update),
                text=self.budget_limit_message
            )
        else:
            result_id = str(uuid4())
            await self.send_inline_query_result(update, result_id, message_content=self.budget_limit_message)

    async def post_init(self, application: Application) -> None:
        """
        Post initialization hook for the bot.
        """
        await application.bot.set_my_commands(self.group_commands, scope=BotCommandScopeAllGroupChats())
        await application.bot.set_my_commands(self.commands)

    def run(self):
        import asyncio as _asyncio

        # 1) Создаём и устанавливаем свой loop
        _loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(_loop)

        # 2) Строим приложение
        application = (
            ApplicationBuilder()
            .token(self.config['token'])
            # добавьте здесь .proxy(...), .base_url(...) при необходимости
            .build()
        )

        # 3) Регистрируем все ваши handler’ы
        #    – сначала командные
        self.register_handlers(application)
        #    – inline-запросы
        application.add_handler(InlineQueryHandler(
            self.inline_query,
            chat_types=[constants.ChatType.PRIVATE,
                        constants.ChatType.GROUP,
                        constants.ChatType.SUPERGROUP]
        ))
        #    – текстовые сообщения (не команды)
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.prompt))
        #    – документы для анализа
        application.add_handler(MessageHandler(filters.Document.ALL, self.analyze))
        #    – обработчик ошибок
        application.add_error_handler(error_handler)

        # 4) Устанавливаем меню команд в Telegram
        _loop.run_until_complete(self.post_init(application))

        # 5) Запускаем polling (единственный раз)
        application.run_polling()


# ==== Knowledge Base handlers (injected) ====
from bot.knowledge_base.reindexer import reindex as kb_reindex
from bot.knowledge_base.yandex_client import YandexDiskClient
from bot.knowledge_base.context_manager import ContextManager
from bot.knowledge_base.retriever import Retriever
from bot.knowledge_base.embedder import Embedder
from bot.knowledge_base.vector_store import VectorStore
from bot.knowledge_base.splitter import build_context_messages

pdf_passwords = {}
ctx_manager = ContextManager()

async def handle_reset(update, context):
    chat_id = update.effective_chat.id
    ctx_manager.reset(chat_id)
    await update.message.reply_text("Контекст очищен.")

async def handle_kb_search(update, context):
    query = " ".join(context.args) if context.args else ""
    if not query:
        await update.message.reply_text("/kb <запрос>")
        return
    results = context.bot_data['retriever'].search(query, top_k=5)
    txt = "\n\n".join([f"{i+1}. {r[:400]}..." for i, r in enumerate(results)]) or "Ничего не найдено"
    await update.message.reply_text(txt)

async def handle_pdfpass(update, context):
    if len(context.args) < 2:
        await update.message.reply_text("Используй: /pdfpass filename.pdf пароль")
        return
    fname = context.args[0]
    pwd = " ".join(context.args[1:])
    pdf_passwords[fname] = pwd
    await update.message.reply_text("Пароль сохранён. Повтори индексацию или запрос.")

async def handle_reindex(update, context):
    await update.message.reply_text("Запускаю переиндексацию...")
    yd = context.bot_data['yd']
    store = context.bot_data['store']
    emb = context.bot_data['embedder']
    async def progress(step,total,file):
        if step % 10 == 0:
            await update.message.reply_text(f"{step}/{total}: {file}")
    added,total_files = await kb_reindex(context.bot_data['root_path'], yd, store, emb, pdf_passwords, progress_cb=None)
    await update.message.reply_text(f"Готово. Обработано файлов: {added}/{total_files}")
