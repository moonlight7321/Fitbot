import os
from dotenv import load_dotenv

load_dotenv()
os.environ.pop("GIGACHAT_MODEL", None)

from gigachat import GigaChat

KEY = os.getenv("GIGACHAT_AUTH_KEY")

with GigaChat(
    credentials=KEY,
    model="GigaChat-3-Ultra",
    scope="GIGACHAT_API_PERS",
    verify_ssl_certs=False,
) as gc:
    resp = gc.chat(
        {
            "model": "GigaChat-3-Ultra",
            "messages": [{"role": "user", "content": "Нарисуй: атлет тренируется в светлом спортзале"}],
            "function_call": "auto",
        }
    )
    content = resp.choices[0].message.content
    print("Ответ модели:", (content or "")[:300])
