"""
mark_pipeline.py

Три задачи:
1. Пометить размерные числа на PDF (красные прямоугольники) + синяя рамка области поиска.
2. Выписать найденные числа в TXT-файл как кандидатов с координатами.
3. Пометить вершины трубопровода в отдельном PDF (цветной кружок + номер V01...).

Использование:
    python mark_pipeline.py <input.pdf> <page_num> [output_prefix]

По умолчанию создаются:
    <prefix>_numbers_marked.pdf   — PDF с обведёнными размерными числами + область поиска
    <prefix>_vertices_marked.pdf  — PDF с отмеченными вершинами
    <prefix>_numbers.txt          — список найденных чисел (кандидатов) и вершин

Пример:
    python mark_pipeline.py "Изометрии.pdf" 1 out
"""

import math
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF
import pdfplumber


# ============================================================
# ОБЛАСТЬ ЧЕРТЕЖА
# ============================================================
# Для A3: X < 770 (таблицы справа), Y < 700 (штамп снизу).
DRAWING_AREA_A3 = fitz.Rect(56.9, 13, 810.0, 705.0)  # (x0, y0, x1, y1)
# Для A4
DRAWING_AREA_A4 = fitz.Rect(56.9, 13, 810.0, 705.0)
# Для A2
DRAWING_AREA_A2 = fitz.Rect(56.9, 13, 810.0, 705.0)


# ============================================================
# ЦВЕТА ВЕРШИН (единый источник истины)
# ============================================================
# role -> RGB (0..1) для PDF
VERTEX_ROLE_COLORS = {
    "endpoint": (0.86, 0.08, 0.15),   # красный — концы
    "corner":   (0.13, 0.55, 0.13),   # зелёный — углы
    "junction": (0.13, 0.35, 0.86),   # синий   — ответвления
}

# role -> название цвета по-русски (для TXT)
VERTEX_ROLE_NAMES_RU = {
    "endpoint": "красный",
    "corner":   "зелёный",
    "junction": "синий",
}

# Пояснения к ролям (для TXT-легенды)
VERTEX_ROLE_DESCRIPTIONS = {
    "endpoint": "концы",
    "corner":   "углы",
    "junction": "ответвления",
}


def get_drawing_area(page):
    w, h = page.rect.width, page.rect.height
    if abs(w - 1190.55) < 10 and abs(h - 841.89) < 10:
        return DRAWING_AREA_A3, "A3"
    if abs(w - 842) < 10 and abs(h - 595) < 10:
        return DRAWING_AREA_A4, "A4"
    if abs(w - 1684) < 10 and abs(h - 1190) < 10:
        return DRAWING_AREA_A2, "A2"
    return fitz.Rect(0, 0, w * 0.65, h * 0.83), f"unknown ({w:.0f}x{h:.0f})"


# ============================================================
# 1. ПОИСК ПРЯМОУГОЛЬНИКОВ (рамок номеров позиций)
# ============================================================

def point_key(x, y, tol=1.0):
    return (round(x / tol) * tol, round(y / tol) * tol)


def find_rectangles(page, drawing_area, min_side=5, max_side=45, tol=1.0):
    """
    Находит все замкнутые 4-угольники (рамки вокруг номеров позиций).
    Возвращает список fitz.Rect.
    """
    drawings = page.get_drawings()
    segments = []
    for d in drawings:
        for item in d["items"]:
            if item[0] == "l":
                p1, p2 = item[1], item[2]
                if not (drawing_area.x0 <= p1.x <= drawing_area.x1 and
                        drawing_area.y0 <= p1.y <= drawing_area.y1 and
                        drawing_area.x0 <= p2.x <= drawing_area.x1 and
                        drawing_area.y0 <= p2.y <= drawing_area.y1):
                    continue
                length = math.hypot(p2.x - p1.x, p2.y - p1.y)
                if min_side <= length <= max_side:
                    segments.append((p1.x, p1.y, p2.x, p2.y))

    graph = defaultdict(list)
    for idx, (x1, y1, x2, y2) in enumerate(segments):
        k1 = point_key(x1, y1, tol)
        k2 = point_key(x2, y2, tol)
        graph[k1].append((idx, k2, (x1, y1), (x2, y2)))
        graph[k2].append((idx, k1, (x2, y2), (x1, y1)))

    rectangles = []
    seen = set()
    for start_key, edges in graph.items():
        for idx1, next_key1, _, p_next1 in edges:
            for idx2, next_key2, _, p_next2 in graph.get(next_key1, []):
                if idx2 == idx1:
                    continue
                for idx3, next_key3, _, p_next3 in graph.get(next_key2, []):
                    if idx3 in (idx1, idx2):
                        continue
                    for idx4, next_key4, _, _ in graph.get(next_key3, []):
                        if idx4 in (idx1, idx2, idx3):
                            continue
                        if next_key4 != start_key:
                            continue
                        points = [edges[0][2], p_next1, p_next2, p_next3]
                        xs = [p[0] for p in points]
                        ys = [p[1] for p in points]
                        bbox = fitz.Rect(min(xs), min(ys), max(xs), max(ys))
                        if not (min_side <= bbox.width <= max_side):
                            continue
                        if not (min_side <= bbox.height <= max_side):
                            continue
                        key = (round(bbox.x0, 0), round(bbox.y0, 0),
                               round(bbox.x1, 0), round(bbox.y1, 0))
                        if key in seen:
                            continue
                        seen.add(key)
                        rectangles.append(bbox)
    return rectangles


def is_inside_any_rect(word_rect, rectangles, tolerance=1.0):
    for r in rectangles:
        if (word_rect.x0 >= r.x0 - tolerance and
            word_rect.y0 >= r.y0 - tolerance and
            word_rect.x1 <= r.x1 + tolerance and
            word_rect.y1 <= r.y1 + tolerance):
            return True
    return False


# ============================================================
# 2. ФИЛЬТРАЦИЯ ЧИСЕЛ
# ============================================================

# Соседи-слова, которые означают «не размер»
FORBIDDEN_NEIGHBORS = {
    "X", "Y", "Z", "Z+", "Z-",
    "mm", "мм", "PE",
    "СМ.", "СМ", "CM.", "CM",
    "СЛИВ", "КОНДЕНСАТА", "КОНДЕНСАТ",
    "ПОДКЛЮЧЕНИЕ", "ПОДКЛЮЧЕНИЯ",
}

# Паттерны для запрещённых соседей
FORBIDDEN_PATTERNS = [
    r"^DN\d+",           # DN40, DN100
    r"^DN\d+X\d+",       # DN40X25
    r"^\d+mm$",          # 40mm
    r"^mm$",
    r"^PE$",
    r"^X\d+$",
    r"^Y\d+$",
    r"^Z[+\-]\d+$",
    r"^V-\d+",           # V-505, V-117
    r"^[А-ЯA-Z]{2}[_-]\d+",  # LC-1031, BB7, LC_1029
    r"^BB\d+$",          # BB7
    r"^LC[_-]\d+",       # LC-1031, LC_1029
    r"^CO[_-]\d+",       # CO_0166
    r"^CB\d+$",
    r"^EB\d+$",
    r"^AB\d+$",
    r"^DB\d+$",
]

DEGREE = "°"


def find_neighbors_same_line(words, idx):
    x0, y0, x1, y1, text, block_no, line_no, word_no = words[idx]
    neighbors = []
    for j, w in enumerate(words):
        if j == idx:
            continue
        nx0, ny0, nx1, ny1, ntext, nb, nl, nw = w
        if nb != block_no or nl != line_no:
            continue
        neighbors.append(ntext.strip())
    return neighbors


def is_coordinate(neighbors):
    for ntext in neighbors:
        if ntext in FORBIDDEN_NEIGHBORS:
            return True
        if DEGREE in ntext:
            return True
        for pat in FORBIDDEN_PATTERNS:
            if re.match(pat, ntext):
                return True
    return False


def extract_dimension_numbers(page, drawing_area):
    words = page.get_text("words")
    rectangles = find_rectangles(page, drawing_area)
    results = []
    discarded = []
    for i, (x0, y0, x1, y1, text, block_no, line_no, word_no) in enumerate(words):
        text = text.strip()
        if not text:
            continue
        word_rect = fitz.Rect(x0, y0, x1, y1)
        if not drawing_area.intersects(word_rect):
            continue
        if not re.fullmatch(r"\d+([.,]\d+)?", text):
            continue
        try:
            val = float(text.replace(",", "."))
        except ValueError:
            continue
        if val < 1 or val > 100000:
            continue
        if is_inside_any_rect(word_rect, rectangles, tolerance=1.0):
            discarded.append((text, word_rect, "inside_rect"))
            continue
        neighbors = find_neighbors_same_line(words, i)
        if is_coordinate(neighbors):
            discarded.append((text, word_rect, f"coord: {neighbors}"))
            continue
        results.append((text, word_rect))
    unique = {}
    for text, r in results:
        key = (text, round(r.x0, 1), round(r.y0, 1))
        if key not in unique:
            unique[key] = (text, r)
    return list(unique.values()), rectangles, discarded


# ============================================================
# 3. ПОИСК ВЕРШИН
# ============================================================

THIN_LW_MAX = 0.8
THICK_LW_MIN = 0.95
MIN_STROKE_LENGTH = 5.0
GRAPH_EPS = 4.0


@dataclass
class Stroke:
    index: int
    x0: float
    y0: float
    x1: float
    y1: float
    length: float
    linewidth: float
    kind: str


def classify_zone(bbox, page_width, page_height):
    x0, y0, x1, y1 = bbox
    cx = (x0 + x1) / 2
    cy = (y0 + y1) / 2
    if cy >= page_height * 0.86:
        return "title_block"
    if cx >= page_width * 0.68 and cy <= page_height * 0.72:
        return "materials_spec"
    if cx >= page_width * 0.68 and cy <= page_height * 0.86:
        return "length_table"
    return "drawing"


def is_frame_line(bbox, length, width, height):
    if length <= 0.5 * max(width, height):
        return False
    margin_x = width * 0.06
    margin_y = height * 0.05
    return (
        min(bbox[0], bbox[2]) < margin_x or
        max(bbox[0], bbox[2]) > width - margin_x or
        min(bbox[1], bbox[3]) < margin_y or
        max(bbox[1], bbox[3]) > height - margin_y
    )


def extract_axis_strokes(pdf_path, page_number):
    """Возвращает список толстых осевых штрихов."""
    strokes = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            if not (1 <= page_number <= len(pdf.pages)):
                return strokes
            page = pdf.pages[page_number - 1]
            width = float(page.width)
            height = float(page.height)
            objects = list(page.objects.get("line", [])) + list(page.objects.get("curve", []))
    except Exception:
        return strokes

    for obj in objects:
        pts = obj.get("pts")
        if not pts or len(pts) < 2:
            continue
        x0, y0 = float(pts[0][0]), float(pts[0][1])
        x1, y1 = float(pts[-1][0]), float(pts[-1][1])
        bbox = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        if classify_zone(bbox, width, height) != "drawing":
            continue
        length = math.hypot(x1 - x0, y1 - y0)
        if length < MIN_STROKE_LENGTH:
            continue
        if is_frame_line(bbox, length, width, height):
            continue
        lw = float(obj.get("linewidth") or obj.get("stroke_width") or 0.0)
        if lw >= THICK_LW_MIN:
            strokes.append(Stroke(
                index=len(strokes),
                x0=x0, y0=y0, x1=x1, y1=y1,
                length=length, linewidth=lw, kind="axis",
            ))
    return strokes


def build_node_graph(axis_strokes):
    nodes = []
    counts = []
    eps2 = GRAPH_EPS * GRAPH_EPS

    def add(ex, ey):
        best = None
        best_d = eps2
        for i, (cx, cy) in enumerate(nodes):
            d = (ex - cx) ** 2 + (ey - cy) ** 2
            if d < best_d:
                best_d = d
                best = i
        if best is None:
            nodes.append([ex, ey])
            counts.append(1)
            return len(nodes) - 1
        total = counts[best] + 1
        nodes[best][0] = (nodes[best][0] * counts[best] + ex) / total
        nodes[best][1] = (nodes[best][1] * counts[best] + ey) / total
        counts[best] = total
        return best

    adjacency = defaultdict(list)
    for si, s in enumerate(axis_strokes):
        na = add(s.x0, s.y0)
        nb = add(s.x1, s.y1)
        if na == nb:
            continue
        adjacency[na].append((nb, si))
        adjacency[nb].append((na, si))
    return nodes, adjacency


def outgoing_vector(stroke, node):
    d_start = math.hypot(stroke.x0 - node[0], stroke.y0 - node[1])
    d_end = math.hypot(stroke.x1 - node[0], stroke.y1 - node[1])
    if d_start <= d_end:
        return stroke.x1 - node[0], stroke.y1 - node[1]
    return stroke.x0 - node[0], stroke.y0 - node[1]


def pipe_component_nodes(adjacency, nodes):
    components = []
    seen = set()
    for start in adjacency:
        if start in seen:
            continue
        component = set()
        queue = [start]
        seen.add(start)
        while queue:
            n = queue.pop()
            component.add(n)
            for nb, _si in adjacency.get(n, []):
                if nb not in seen:
                    seen.add(nb)
                    queue.append(nb)
        components.append(component)
    if not components:
        return set()
    main = max(components, key=len)
    main_x = [nodes[i][0] for i in main]
    main_y = [nodes[i][1] for i in main]
    main_bbox = (min(main_x), min(main_y), max(main_x), max(main_y))

    def bbox_gap(component):
        xs = [nodes[i][0] for i in component]
        ys = [nodes[i][1] for i in component]
        bbox = (min(xs), min(ys), max(xs), max(ys))
        dx = max(main_bbox[0] - bbox[2], bbox[0] - main_bbox[2], 0.0)
        dy = max(main_bbox[1] - bbox[3], bbox[1] - main_bbox[3], 0.0)
        return math.hypot(dx, dy)

    allowed = set()
    for component in components:
        if component is main or bbox_gap(component) <= 55.0:
            allowed.update(component)
    return allowed


def extract_vertices(pdf_path, page_number):
    """
    Возвращает список вершин: список dict с x, y, role, degree.
    """
    axis_strokes = extract_axis_strokes(pdf_path, page_number)
    if not axis_strokes:
        return []

    nodes, adjacency = build_node_graph(axis_strokes)
    allowed = pipe_component_nodes(adjacency, nodes)

    vertices = []
    for node_index, connected in adjacency.items():
        if node_index not in allowed or not connected:
            continue
        degree = len(connected)
        is_corner = False
        if degree == 2:
            first = axis_strokes[connected[0][1]]
            second = axis_strokes[connected[1][1]]
            dx1, dy1 = outgoing_vector(first, nodes[node_index])
            dx2, dy2 = outgoing_vector(second, nodes[node_index])
            l1 = math.hypot(dx1, dy1)
            l2 = math.hypot(dx2, dy2)
            if l1 > 0 and l2 > 0:
                cosine = (dx1 * dx2 + dy1 * dy2) / (l1 * l2)
                is_corner = cosine > -0.94
        if degree == 1:
            role = "endpoint"
        elif degree >= 3:
            role = "junction"
        elif is_corner:
            role = "corner"
        else:
            continue
        x, y = nodes[node_index]
        vertices.append({
            "x": round(x, 2),
            "y": round(y, 2),
            "role": role,
            "degree": degree,
        })
    return merge_vertex_clusters(vertices)


def merge_vertex_clusters(vertices, radius=20.0):
    if not vertices:
        return []
    merged = []
    for role in ("endpoint", "corner", "junction"):
        role_vertices = [v for v in vertices if v["role"] == role]
        clusters = []
        for v in role_vertices:
            matching = [
                ci for ci, cluster in enumerate(clusters)
                if any(math.hypot(v["x"] - item["x"], v["y"] - item["y"]) <= radius for item in cluster)
            ]
            if not matching:
                clusters.append([v])
                continue
            target = clusters[matching[0]]
            target.append(v)
            for ci in reversed(matching[1:]):
                target.extend(clusters.pop(ci))

        for cluster in clusters:
            # Два близких endpoint без угла между ними — это разрыв контура
            if role == "endpoint" and len(cluster) > 1:
                continue
            x = sum(item["x"] for item in cluster) / len(cluster)
            y = sum(item["y"] for item in cluster) / len(cluster)
            merged.append({
                "x": round(x, 2),
                "y": round(y, 2),
                "role": role,
                "degree": cluster[0]["degree"],
            })
    return merged


# ============================================================
# 4. РЕНДЕР И СОХРАНЕНИЕ
# ============================================================

def save_numbers_pdf(input_pdf, page_num, output_pdf, dims, rectangles, drawing_area):
    """PDF с обведёнными размерными числами (красным), рамками (серым)
    и границей области поиска (синий пунктир)."""
    doc = fitz.open(input_pdf)
    new_doc = fitz.open()
    new_page = new_doc.new_page(width=doc[page_num].rect.width,
                                height=doc[page_num].rect.height)
    new_page.show_pdf_page(new_page.rect, doc, page_num)

    # Граница области поиска — синий пунктир
    new_page.draw_rect(
        drawing_area,
        color=(0, 0.3, 1.0),
        width=1.5,
        dashes="[4 3] 0",
    )
    new_page.insert_text(
        fitz.Point(drawing_area.x0 + 4, drawing_area.y0 + 14),
        f"SEARCH AREA  x=[{drawing_area.x0:.0f}..{drawing_area.x1:.0f}]  "
        f"y=[{drawing_area.y0:.0f}..{drawing_area.y1:.0f}]",
        fontsize=9, fontname="helv", color=(0, 0.3, 1.0),
    )

    # Рамки номеров позиций — серым, тонко
    for r in rectangles:
        new_page.draw_rect(r, color=(0.6, 0.6, 0.6), width=0.5)

    # Размерные числа — красным, жирно
    for text, rect in dims:
        pad = 2
        r = fitz.Rect(rect.x0 - pad, rect.y0 - pad, rect.x1 + pad, rect.y1 + pad)
        new_page.draw_rect(r, color=(1, 0, 0), width=1.5)

    new_doc.save(output_pdf)
    new_doc.close()
    doc.close()


def save_vertices_pdf(input_pdf, page_num, output_pdf, vertices):
    """PDF с отмеченными вершинами: цветной кружок + номер V01, V02, ..."""
    doc = fitz.open(input_pdf)
    new_doc = fitz.open()
    page = doc[page_num]
    new_page = new_doc.new_page(width=page.rect.width, height=page.rect.height)
    new_page.show_pdf_page(new_page.rect, doc, page_num)

    for idx, v in enumerate(vertices, start=1):
        color = VERTEX_ROLE_COLORS.get(v["role"], (0, 0, 0))
        center = fitz.Point(v["x"], v["y"])
        new_page.draw_circle(center, 5, color=color, fill=color, width=1.2)
        new_page.insert_text(
            fitz.Point(v["x"] + 8, v["y"] - 8),
            f"V{idx:02d}",
            fontsize=9, fontname="helv", color=color,
        )

    new_doc.save(output_pdf)
    new_doc.close()
    doc.close()


def save_numbers_txt(output_txt, dims, vertices, drawing_area):
    """
    TXT со списком найденных чисел-кандидатов и вершин.
    Без прямоугольников.
    """
    with open(output_txt, "w", encoding="utf-8") as f:
        # ---------- ЧИСЛА ----------
        f.write("# Найденные размерные числа (кандидаты)\n")
        f.write(f"# Область поиска: x=[{drawing_area.x0:.1f}..{drawing_area.x1:.1f}]  "
                f"y=[{drawing_area.y0:.1f}..{drawing_area.y1:.1f}]\n")
        f.write(f"# Всего чисел: {len(dims)}\n")
        f.write("# Формат: text\tx0\ty0\tx1\ty1\tcenter_x\tcenter_y\n\n")
        for text, r in dims:
            cx = (r.x0 + r.x1) / 2
            cy = (r.y0 + r.y1) / 2
            f.write(f"{text}\t{r.x0:.1f}\t{r.y0:.1f}\t{r.x1:.1f}\t{r.y1:.1f}\t"
                    f"{cx:.1f}\t{cy:.1f}\n")

        # ---------- ВЕРШИНЫ ----------
        f.write("\n\n# ============================================================\n")
        f.write("# Найденные вершины трубопровода\n")
        f.write("# ============================================================\n")
        f.write("# Пояснения к цветам:\n")
        f.write("#   красный  — концы (endpoint)\n")
        f.write("#   синий    — ответвления (junction)\n")
        f.write("#   зелёный  — углы (corner)\n")
        f.write("#\n")
        f.write(f"# Всего вершин: {len(vertices)}\n")
        f.write("# Формат: id\trole\tcolor\tx\ty\tdegree\n\n")
        for idx, v in enumerate(vertices, start=1):
            color_name = VERTEX_ROLE_NAMES_RU.get(v["role"], "чёрный")
            f.write(f"V{idx:02d}\t{v['role']}\t{color_name}\t"
                    f"{v['x']:.2f}\t{v['y']:.2f}\t{v['degree']}\n")


# ============================================================
# 5. MAIN
# ============================================================

def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    input_pdf = sys.argv[1]
    try:
        page_num = int(sys.argv[2])
    except ValueError:
        print("Ошибка: номер страницы должен быть целым числом")
        sys.exit(1)

    if len(sys.argv) >= 4:
        prefix = sys.argv[3]
    else:
        prefix = Path(input_pdf).stem + f"_page{page_num}"

    output_numbers_pdf = f"{prefix}_numbers_marked.pdf"
    output_vertices_pdf = f"{prefix}_vertices_marked.pdf"
    output_txt = f"{prefix}_numbers.txt"

    if not Path(input_pdf).exists():
        print(f"Ошибка: файл {input_pdf} не найден")
        sys.exit(1)

    # --- 1. Открываем PDF через PyMuPDF ---
    doc = fitz.open(input_pdf)
    if page_num < 0 or page_num >= len(doc):
        print(f"Ошибка: страница {page_num} вне диапазона (0..{len(doc)-1})")
        sys.exit(1)

    page = doc[page_num]
    drawing_area, fmt = get_drawing_area(page)
    print(f"Формат: {fmt}")
    print(f"Область поиска: x=[{drawing_area.x0:.1f}..{drawing_area.x1:.1f}]  "
          f"y=[{drawing_area.y0:.1f}..{drawing_area.y1:.1f}]")

    # --- 2. Ищем размерные числа ---
    print("\n=== Поиск размерных чисел ===")
    dims, rectangles, discarded = extract_dimension_numbers(page, drawing_area)
    print(f"Найдено размерных чисел: {len(dims)}")
    for text, r in dims:
        print(f"  {text:>6}  bbox=({r.x0:6.1f}, {r.y0:6.1f}, {r.x1:6.1f}, {r.y1:6.1f})")

    doc.close()

    # --- 3. Ищем вершины ---
    print(f"\n=== Поиск вершин трубопровода ===")
    vertices = extract_vertices(input_pdf, page_num + 1)  # pdfplumber 1-based
    print(f"Найдено вершин: {len(vertices)}")
    for idx, v in enumerate(vertices, start=1):
        color_name = VERTEX_ROLE_NAMES_RU.get(v["role"], "чёрный")
        print(f"  V{idx:02d}  x={v['x']:7.1f}  y={v['y']:7.1f}  "
              f"role={v['role']:8s} ({color_name})")

    # --- 4. Сохраняем PDF с числами ---
    print(f"\n=== Сохранение PDF с числами ===")
    save_numbers_pdf(input_pdf, page_num, output_numbers_pdf, dims, rectangles, drawing_area)
    print(f"Сохранено: {output_numbers_pdf}")

    # --- 5. Сохраняем TXT с числами и вершинами ---
    print(f"\n=== Сохранение TXT с числами и вершинами ===")
    save_numbers_txt(output_txt, dims, vertices, drawing_area)
    print(f"Сохранено: {output_txt}")

    # --- 6. Сохраняем PDF с вершинами ---
    print(f"\n=== Сохранение PDF с вершинами ===")
    save_vertices_pdf(input_pdf, page_num, output_vertices_pdf, vertices)
    print(f"Сохранено: {output_vertices_pdf}")

    print("\n=== Готово ===")
    print(f"  Числа (PDF):    {output_numbers_pdf}")
    print(f"  Числа+Вершины (TXT): {output_txt}")
    print(f"  Вершины (PDF):  {output_vertices_pdf}")


if __name__ == "__main__":
    main()