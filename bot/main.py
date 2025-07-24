# bot/main.py
import asyncio
import logging
import os

from dotenv import load_dotenv
from telegram import BotCommand
from telegram.ext import ApplicationBuilder

from bot.telegram_bot import ChatGPTTelegramBot
from bot.openai_helper import OpenAIHelper
from bot.plugin_manager import PluginManager

# Если добавили трассер
try:
    from bot.error_tracer import init_error_tracer
except Exception:  # pragma: no cover
    def init_error_tracer():
        logging.info("Sentry not configured or not installed")


def setup_logging():
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
    )


async def set_commands(application, enable_image: bool, enable_tts: bool):
    commands = [
        BotCommand("start", "помощь"),
        BotCommand("help", "помощь"),
        BotCommand("reset", "сброс диалога"),
        BotCommand("kb", "база знаний / поиск"),
        BotCommand("pdfpass", "ввести пароль к PDF"),
        BotCommand("list_models", "показать модели"),
        BotCommand("set_model", "выбрать модель"),
        BotCommand("analyze", "проанализировать последний документ/фото"),
    ]
    if enable_image:
        commands.append(BotCommand("image", "сгенерировать изображение"))
    if enable_tts:
        commands.append(BotCommand("tts", "синтез речи"))

    await application.bot.set_my_commands(commands)


def main():
    load_dotenv()
    setup_logging()
    init_error_tracer()

    # ------- OpenAI / Plugins config -------
    openai_config = {
        "api_key": os.environ["OPENAI_API_KEY"],
        "model": os.environ.get("OPENAI_MODEL", "gpt-4o"),
        "vision_model": os.environ.get("VISION_MODEL", "gpt-4o"),
        "image_model": os.environ.get("IMAGE_MODEL", "gpt-image-1"),
        "image_size": os.environ.get("IMAGE_SIZE", "1024x1024"),
        "tts_model": os.environ.get("TTS_MODEL", "gpt-4o-mini-tts"),
        "tts_voice": os.environ.get("TTS_VOICE", "alloy"),
        "temperature": float(os.environ.get("OPENAI_TEMPERATURE", "0.7")),
        "n_choices": int(os.environ.get("N_CHOICES", "1")),
        "max_tokens": int(os.environ.get("MAX_TOKENS", "1024")),
        "presence_penalty": float(os.environ.get("PRESENCE_PENALTY", "0")),
        "frequency_penalty": float(os.environ.get("FREQUENCY_PENALTY", "0")),
        "assistant_prompt": os.environ.get("ASSISTANT_PROMPT", "You are a helpful assistant."),
        "max_history_size": int(os.environ.get("MAX_HISTORY_SIZE", "20")),
        "max_conversation_age_minutes": int(os.environ.get("MAX_CONVERSATION_AGE", "60")),
        "enable_functions": os.environ.get("ENABLE_FUNCTIONS", "false").lower() == "true",
        "show_usage": os.environ.get("SHOW_USAGE", "true").lower() == "true",
        "show_plugins_used": os.environ.get("SHOW_PLUGINS_USED", "false").lower() == "true",
        "enable_vision_follow_up_questions": os.environ.get("VISION_FOLLOWUP", "false").lower() == "true",
        "vision_max_tokens": int(os.environ.get("VISION_MAX_TOKENS", "1024")),
        "vision_prompt": os.environ.get("VISION_PROMPT", "Опиши, что на изображении."),
        "vision_detail": os.environ.get("VISION_DETAIL", "auto"),
        "whisper_prompt": os.environ.get("WHISPER_PROMPT", ""),
        "bot_language": os.environ.get("BOT_LANGUAGE", "ru"),
        "proxy": os.environ.get("PROXY", None),
        "enable_image_generation": os.environ.get("ENABLE_IMAGE_GENERATION", "true").lower() == "true",
        "enable_tts_generation": os.environ.get("ENABLE_TTS_GENERATION", "false").lower() == "true",
        "functions_max_consecutive_calls": int(os.environ.get("FUNCTIONS_MAX_CONSECUTIVE_CALLS", "3")),
    }

    plugin_config = {
        # добавьте настройки плагинов, если используете
    }

    telegram_config = {
        "token": os.environ["TELEGRAM_BOT_TOKEN"],
        "enable_image_generation": openai_config["enable_image_generation"],
        "enable_tts_generation": openai_config["enable_tts_generation"],
        "allowed_models": os.environ.get("ALLOWED_MODELS", "").split(",") if os.environ.get("ALLOWED_MODELS") else None,
    }

    plugin_manager = PluginManager(config=plugin_config)
    openai_helper = OpenAIHelper(config=openai_config, plugin_manager=plugin_manager)

    bot = ChatGPTTelegramBot(config=telegram_config, openai_helper=openai_helper)

    # Собираем PTB-приложение здесь, чтобы выставить команды до run_polling
    application = ApplicationBuilder().token(telegram_config["token"]).build()

    # Регистрируем хендлеры бота
    bot.register_handlers(application)

    # Устанавливаем команды
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(set_commands(application,
                                         enable_image=telegram_config["enable_image_generation"],
                                         enable_tts=telegram_config["enable_tts_generation"]))

    # post_init (если что-то нужно)
    loop.run_until_complete(bot.post_init(application))

    # Стартуем
    application.run_polling()


if __name__ == "__main__":
    main()
