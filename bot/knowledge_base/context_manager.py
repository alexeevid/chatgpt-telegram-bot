from dataclasses import dataclass, field
from typing import List, Dict

@dataclass
class SessionContext:
    chunks: List[str] = field(default_factory=list)
    history: List[dict] = field(default_factory=list)

class ContextManager:
    def __init__(self):
        self.sessions: Dict[int, SessionContext] = {}

    def get(self, chat_id: int) -> SessionContext:
        return self.sessions.setdefault(chat_id, SessionContext())

    def reset(self, chat_id: int):
        self.sessions.pop(chat_id, None)
