import os
from datetime import datetime

from sqlalchemy import Column, Integer, String, DateTime, Text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base

# Загружаем URL из переменной окружения
DATABASE_URL = os.getenv("DATABASE_URL")

# Создаём асинхронный движок и сессию
engine = create_async_engine(DATABASE_URL, echo=True)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Базовый класс для моделей
Base = declarative_base()

# Модель документа
class Document(Base):
    __tablename__ = "documents"
    
    id = Column(Integer, primary_key=True, index=True)
    yandex_path = Column(String, unique=True, index=True)
    title = Column(String)
    uploaded_at = Column(DateTime, default=datetime.utcnow)
    full_text = Column(Text)
