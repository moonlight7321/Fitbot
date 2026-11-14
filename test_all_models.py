import os
from dotenv import load_dotenv

load_dotenv()
os.environ.pop("GIGACHAT_MODEL", None)  # убираем переменную, если она есть — не должна мешать

from gigachat import GigaChat

KEY = os.getenv("GIGACHAT_AUTH_KEY")

with GigaChat(
    credentials=KEY,
    scope="GIGACHAT_API_PERS",
    verify_ssl_certs=False,
) as gc:
    names = [m.id_ for m in gc.get_models().data]
    print("Список моделей:", names)

    for name in names:
        if name.lower().startswith("embed"):
            continue  # embeddings — это не чат-модели, пропускаем
        try:
            resp = gc.chat(
                {
                    "model": name,
                    "messages": [{"role": "user", "content": "Ответь одним словом: привет"}],
                }
            )
            text = (resp.choices[0].message.content or "")[:60]
            print(f"[OK]   {name}: {text}")
        except Exception as e:
            msg = str(e)[:120]
            print(f"[FAIL] {name}: {msg}")
