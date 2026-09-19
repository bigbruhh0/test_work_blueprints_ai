from __future__ import annotations

import base64
import io
import json
from typing import Any

import requests

from .pre import label_vertices_on_image, render_page_image


GRAPH_PROMPT = """Ты работаешь с изометрическим чертежом трубопровода.
На изображении поверх чертёжа красными точками и подписями V01, V02, ... отмечены вершины
трубы: концы, углы (повороты) и стыки. Дляа помечены программой, не добавляй и не удаляй их.

Задача: восстановить основной путь трассы как последовательность вершин и найти РАССТОЯНИЕ
между каждой парой СМЕЖНЫХ вершин, глядя на размерные линии самого чертежа.

Входные данные:
- изображение листа с нанесёнными вершинами;
- JSON: список вершин (id, роль, координаты на изображении);
- JSON: список чисел, найденных в графической зоне чертежа (id, text, bbox).

Верни строго JSON без Markdown в таком виде:
{
  "order": ["V01", "V02", ...],
  "edges": [
    {
      "from": "V01",
      "to": "V02",
      "length_mm": 60,
      "exact": true,
      "basis": "candidate_id C-002-0026 или описание связи с участком",
      "note": ""
    }
  ],
  "branch_edges": [],
  "unclear": [
    { "text": "что видно", "bbox": [0, 0, 0, 0], "why": "почему неопределённо" }
  ],
  "notes": []
}

Жёсткие правила:
1. Используешь ТОЛЬКО графическую часть чертежа: ось трубы, размерные линии со стрелками,
   выноски и числа на них. ЗАПРЕЩЕНО использовать или вычислять что-либо вне графической
   части: таблицу «Длины отрезков», спецификацию материалов, штамп, легенды, координатные
   блоки X/Y/Z и любые числа вне чертежа.
2. Нельзя придумывать числа. Если значение не читается или непонятно, к какому участку
   оно относится — length_mm=null, exact=false, и обязательно добавь запись в "unclear"
   с тем, ЧТО ты видишь (текст/цифра и примерно где).
3. Размер участка ищи на размерной линии со стрелками/засечками, которая принадлежит участку
   между двумя конкретными вершинами. Размер может быть вынесенным: число -> выносная стрелка
   -> размерная стрелка к концу участка. Проследи эту цепочку и ссылайся на неё в "basis".
4. Один размерный кандидат может использоваться только один раз.
5. Числа возле DN-подписей (DN40 и т.п.), координатных блоков X/Y/Z, меток опор или позиций
   (О4, <3>, «СМ.») не являются расстояниями между вершинами.
6. Если к одному участку относятся несколько размеров, бери тот, чья размерная линия
   коллинеарна оси участка. Короткая параллельная линия рядом с основной размерной —
   привязка опоры/детали, а не длина.
7. Между вершинами нельзя строить ребро через промежуточную вершину: отмечай участки
   между СМЕЖНЫМИ вершинами отдельно.
8. Если между вершинами нет расстояния на чертеже (обрыв, переход на другой лист) —
   всё равно создай ребро с length_mm=null и укажи это в note.
9. В "order" укажи последовательность вершин основного пути от старта к концу. Если порядок
   надёжно не восстанавливается, перечисли те рёбра, которые уверенны.
"""


def build_images(pdf_path: str, page_number: int, vertices: list[dict]) -> list[Any]:
    base = render_page_image(pdf_path, page_number, zoom=1.5)
    return [label_vertices_on_image(base, vertices, zoom=1.5)]


def _data_url(image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def build_content(pdf_path: str, page_number: int, vertices: list[dict], numbers: list[dict]) -> list[dict]:
    content: list[dict] = [
        {
            "type": "text",
            "text": GRAPH_PROMPT
            + f"\n\nВершины:\n{json.dumps(vertices, ensure_ascii=False, indent=2)}"
            + f"\n\nЧисловые кандидаты графической зоны:\n{json.dumps(numbers, ensure_ascii=False, indent=2)}",
        }
    ]
    for image in build_images(pdf_path, page_number, vertices):
        content.append({"type": "image_url", "image_url": {"url": _data_url(image)}})
    return content


def call_deepseek(
    api_key: str,
    model: str,
    pdf_path: str,
    page_number: int,
    vertices: list[dict],
    numbers: list[dict],
    base_url: str = "https://api.deepseek.com/chat/completions",
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Ты как разметка расстояний по вершинам изометрического чертежа. Возвращай только JSON."},
            {"role": "user", "content": build_content(pdf_path, page_number, vertices, numbers)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
    }
    response = requests.post(
        base_url,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=240,
    )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"]["content"]
    return _parse_json(content)


def _parse_json(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        return json.loads(text)
    except ValueError:
        decoder = json.JSONDecoder()
        for index, char in enumerate(text):
            if char == "{":
                try:
                    obj, _end = decoder.raw_decode(text[index:])
                    return obj
                except ValueError:
                    continue
        raise
