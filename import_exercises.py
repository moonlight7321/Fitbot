"""Скачивает картинки упражнений из free-exercise-db (Unlicense).

Запускается вручную один раз: создаёт media/exercises/<slug>/0.jpg и 1.jpg.
Повторный запуск докачивает только отсутствующие файлы.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

BASE_URL = "https://raw.githubusercontent.com/yuhonas/free-exercise-db/main/exercises/"
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "exercises.json"
MEDIA = ROOT / "media" / "exercises"


def download(url: str, target: Path) -> int:
    with urllib.request.urlopen(url, timeout=30) as response:
        data = response.read()
    if not data.startswith(b"\xff\xd8"):
        raise ValueError(f"это не JPEG: {url}")
    target.write_bytes(data)
    return len(data)


def main() -> int:
    items = json.loads(DATA.read_text(encoding="utf-8"))
    MEDIA.mkdir(parents=True, exist_ok=True)
    downloaded = skipped = failed = 0

    for item in items:
        slug = item["slug"]
        folder = MEDIA / slug
        folder.mkdir(parents=True, exist_ok=True)
        for idx in (0, 1):
            target = folder / f"{idx}.jpg"
            if target.exists() and target.stat().st_size > 1024:
                skipped += 1
                continue
            try:
                size = download(f"{BASE_URL}{slug}/{idx}.jpg", target)
                downloaded += 1
                print(f"  {slug}/{idx}.jpg — {size // 1024} КБ")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"  ОШИБКА {slug}/{idx}: {exc}")

    print(f"\nскачано: {downloaded}, уже было: {skipped}, ошибок: {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
