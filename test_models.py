import os
from dotenv import load_dotenv

load_dotenv()

from gigachat import GigaChat

with GigaChat(
    credentials=os.getenv("GIGACHAT_AUTH_KEY"),
    scope="GIGACHAT_API_PERS",
    verify_ssl_certs=False,
) as gc:
    print("Модели, доступные твоему ключу:")
    for m in gc.get_models().data:
        print(" -", m.id_)
