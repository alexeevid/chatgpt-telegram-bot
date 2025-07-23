from typing import List
import tiktoken

def split_text(text: str, max_tokens: int = 500, overlap: int = 50, model: str="gpt-4o-mini") -> List[str]:
    enc = tiktoken.encoding_for_model(model)
    tokens = enc.encode(text)
    chunks = []
    start = 0
    while start < len(tokens):
        end = min(start + max_tokens, len(tokens))
        chunk = enc.decode(tokens[start:end])
        chunks.append(chunk)
        start = end - overlap
        if start < 0:
            start = 0
    return chunks

def num_tokens(messages, model="gpt-4o-mini"):
    enc = tiktoken.encoding_for_model(model)
    total = 0
    for m in messages:
        total += len(enc.encode(m.get("content","")))
    return total

def trim_to_token_limit(messages, max_tokens: int, model="gpt-4o-mini"):
    enc = tiktoken.encoding_for_model(model)
    while num_tokens(messages, model) > max_tokens and len(messages) > 1:
        # удаляем самое старое пользовательское/ассистентское сообщение (оставляем system)
        for i, m in enumerate(messages):
            if m["role"] != "system":
                del messages[i]
                break
    return messages

def build_context_messages(chunks: List[str]) -> List[dict]:
    if not chunks:
        return []
    context_text = "\n\n".join([f"[DOC {i+1}] {c}" for i, c in enumerate(chunks)])
    return [{"role": "system", "content": "Ниже даны фрагменты из базы знаний. Используй их для ответа, ссылайся на документы в квадратных скобках."},
            {"role": "user", "content": context_text}]
