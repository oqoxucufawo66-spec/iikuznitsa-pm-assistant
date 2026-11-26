from __future__ import annotations
import json
from pathlib import Path
from typing import Any
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt
from app.config import ARTIFACTS_DIR
from app.db import get_conn, now_ts

NAVY = RGBColor(15, 34, 64)
ACCENT = RGBColor(37, 99, 235)
SLATE = RGBColor(71, 85, 105)
DARK = RGBColor(17, 24, 39)
LIGHT_BG = RGBColor(241, 245, 249)
AMBER_BG = RGBColor(254, 243, 199)
AMBER_TXT = RGBColor(146, 64, 14)
WHITE = RGBColor(255, 255, 255)


def _add_header(slide, title: str, badge_text: str = "ДЕМО-ДАННЫЕ · ИИ-помощник ПМ") -> None:
    # Top accent bar
    bar = slide.shapes.add_shape(1, Inches(0), Inches(0), Inches(13.333), Inches(0.18))
    bar.fill.solid()
    bar.fill.fore_color.rgb = ACCENT
    bar.line.fill.background()

    # Title
    tb = slide.shapes.add_textbox(Inches(0.6), Inches(0.32), Inches(9.0), Inches(0.75))
    tf = tb.text_frame
    tf.word_wrap = True
    p = tf.paragraphs[0]
    p.text = title
    p.font.size = Pt(24)
    p.font.bold = True
    p.font.color.rgb = NAVY

    # Demo badge
    badge = slide.shapes.add_shape(5, Inches(9.7), Inches(0.35), Inches(3.1), Inches(0.42))
    badge.fill.solid()
    badge.fill.fore_color.rgb = AMBER_BG
    badge.line.fill.background()
    btf = badge.text_frame
    btf.word_wrap = True
    bp = btf.paragraphs[0]
    bp.text = badge_text
    bp.alignment = PP_ALIGN.CENTER
    bp.font.size = Pt(10)
    bp.font.bold = True
    bp.font.color.rgb = AMBER_TXT


def _add_bullets(slide, left: float, top: float, width: float, height: float, heading: str, items: list[str]) -> None:
    box = slide.shapes.add_shape(5, Inches(left), Inches(top), Inches(width), Inches(height))
    box.fill.solid()
    box.fill.fore_color.rgb = LIGHT_BG
    box.line.fill.background()

    tb = slide.shapes.add_textbox(Inches(left + 0.25), Inches(top + 0.2), Inches(width - 0.5), Inches(height - 0.4))
    tf = tb.text_frame
    tf.word_wrap = True
    p0 = tf.paragraphs[0]
    p0.text = heading
    p0.font.size = Pt(16)
    p0.font.bold = True
    p0.font.color.rgb = ACCENT
    p0.space_after = Pt(8)

    for item in items:
        p = tf.add_paragraph()
        p.text = f"• {item}"
        p.font.size = Pt(13.5)
        p.font.color.rgb = DARK
        p.space_after = Pt(6)


def generate_sales_pptx(sku: str) -> dict[str, Any]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM products WHERE sku=?", (sku,)).fetchone()
        if not row:
            raise ValueError(f"Товар с SKU {sku} не найден")
        product = dict(row)
        alt_row = conn.execute(
            "SELECT * FROM products WHERE category=? AND sku!=? ORDER BY ABS(price_rub - ?) ASC LIMIT 1",
            (product["category"], sku, product["price_rub"]),
        ).fetchone()
        alt = dict(alt_row) if alt_row else None

    specs = json.loads(product["specs_json"])
    benefits = json.loads(product["key_benefits_json"])
    objections = json.loads(product["objections_json"])
    stock_str = "В наличии" if product["in_stock"] else "Нет в наличии (уточнять у ПМ)"

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    blank = prs.slide_layouts[6]

    # Slide 1: Cover
    s1 = prs.slides.add_slide(blank)
    bg = s1.shapes.add_shape(1, Inches(0), Inches(0), Inches(13.333), Inches(7.5))
    bg.fill.solid()
    bg.fill.fore_color.rgb = NAVY
    bg.line.fill.background()

    badge1 = s1.shapes.add_shape(5, Inches(0.8), Inches(0.8), Inches(4.6), Inches(0.45))
    badge1.fill.solid()
    badge1.fill.fore_color.rgb = AMBER_BG
    badge1.line.fill.background()
    bp1 = badge1.text_frame.paragraphs[0]
    bp1.text = "ДЕМО-ДАННЫЕ · Материал для отдела продаж"
    bp1.alignment = PP_ALIGN.CENTER
    bp1.font.size = Pt(12)
    bp1.font.bold = True
    bp1.font.color.rgb = AMBER_TXT

    tb1 = s1.shapes.add_textbox(Inches(0.8), Inches(1.7), Inches(11.5), Inches(2.2))
    tf1 = tb1.text_frame
    tf1.word_wrap = True
    p = tf1.paragraphs[0]
    p.text = product["name"]
    p.font.size = Pt(36)
    p.font.bold = True
    p.font.color.rgb = WHITE

    sub = tf1.add_paragraph()
    sub.text = f"Артикул: {product['sku']}  |  Бренд: {product['brand']}  |  Категория: {product['category']}"
    sub.font.size = Pt(18)
    sub.font.color.rgb = RGBColor(191, 219, 254)
    sub.space_before = Pt(14)

    kpi_tb = s1.shapes.add_textbox(Inches(0.8), Inches(4.8), Inches(11.5), Inches(1.5))
    ktf = kpi_tb.text_frame
    kp = ktf.paragraphs[0]
    kp.text = f"Цена: {product['price_rub']:,} ₽   •   Гарантия: {product['warranty_months']} мес.   •   Статус: {stock_str}".replace(",", " ")
    kp.font.size = Pt(22)
    kp.font.bold = True
    kp.font.color.rgb = RGBColor(147, 197, 253)

    # Slide 2: Benefits & Audience
    s2 = prs.slides.add_slide(blank)
    _add_header(s2, f"Ценность для клиента: {product['sku']}")
    _add_bullets(
        s2, 0.6, 1.35, 6.2, 5.4,
        "Ключевые преимущества (УТП)",
        benefits or ["См. карточку товара в базе знаний"],
    )
    _add_bullets(
        s2, 7.1, 1.35, 5.6, 5.4,
        "Кому предлагать и комплектация",
        [
            f"Целевая аудитория: {product['target_audience'] or 'Уточнить у ПМ'}",
            f"Комплектация: {product['package_contents'] or 'Стандартная'}",
            f"Гарантия: {product['warranty_months']} мес.",
            f"Статус склада: {stock_str}",
        ],
    )

    # Slide 3: Technical specs table
    s3 = prs.slides.add_slide(blank)
    _add_header(s3, f"Технические характеристики: {product['name']}")
    spec_items = list(specs.items())[:8]
    rows_cnt = max(2, len(spec_items) + 1)
    table_shape = s3.shapes.add_table(rows_cnt, 2, Inches(0.6), Inches(1.35), Inches(12.1), Inches(5.4))
    tbl = table_shape.table
    tbl.columns[0].width = Inches(4.3)
    tbl.columns[1].width = Inches(7.8)
    tbl.cell(0, 0).text = "Параметр"
    tbl.cell(0, 1).text = "Значение (из карточки)"
    for c in range(2):
        cell = tbl.cell(0, c)
        cell.fill.solid()
        cell.fill.fore_color.rgb = NAVY
        p = cell.text_frame.paragraphs[0]
        p.font.bold = True
        p.font.size = Pt(13)
        p.font.color.rgb = WHITE

    for r_idx, (k, v) in enumerate(spec_items, 1):
        tbl.cell(r_idx, 0).text = str(k)
        tbl.cell(r_idx, 1).text = str(v)
        for c in range(2):
            p = tbl.cell(r_idx, c).text_frame.paragraphs[0]
            p.font.size = Pt(12.5)
            p.font.color.rgb = DARK

    # Slide 4: Objections handling
    s4 = prs.slides.add_slide(blank)
    _add_header(s4, "Отработка частых возражений клиентов")
    obj_lines = [f"«{o.get('objection', '')}» → {o.get('answer', '')}" for o in objections]
    if not obj_lines:
        obj_lines = [
            "«Есть ли гарантия и возврат?» → Гарантия по карточке + возврат физлицам в течение 14 дней по FAQ.",
            "«Можно ли оплатить по СБП или от юрлица?» → Доступны МИР/СБП/рассрочка 0% и безнал по счёту с ЭДО.",
        ]
    _add_bullets(s4, 0.6, 1.35, 12.1, 5.4, "Готовые аргументы для менеджера", obj_lines)

    # Slide 5: Comparison with nearest alternative
    s5 = prs.slides.add_slide(blank)
    _add_header(s5, "Сравнение с альтернативой в каталоге")
    if alt:
        alt_specs = json.loads(alt["specs_json"])
        comp_rows = [
            ("Показатель", product["name"], alt["name"]),
            ("Артикул", product["sku"], alt["sku"]),
            ("Цена", f"{product['price_rub']:,} ₽".replace(",", " "), f"{alt['price_rub']:,} ₽".replace(",", " ")),
            ("Гарантия", f"{product['warranty_months']} мес.", f"{alt['warranty_months']} мес."),
        ]
        for k in list(specs.keys())[:4]:
            comp_rows.append((k, str(specs.get(k, "—")), str(alt_specs.get(k, "—"))))
        t5 = s5.shapes.add_table(len(comp_rows), 3, Inches(0.6), Inches(1.35), Inches(12.1), Inches(5.4)).table
        t5.columns[0].width = Inches(3.1)
        t5.columns[1].width = Inches(4.5)
        t5.columns[2].width = Inches(4.5)
        for r_i, row_vals in enumerate(comp_rows):
            for c_i, val in enumerate(row_vals):
                cell = t5.cell(r_i, c_i)
                cell.text = val
                p = cell.text_frame.paragraphs[0]
                p.font.size = Pt(12)
                if r_i == 0:
                    cell.fill.solid()
                    cell.fill.fore_color.rgb = NAVY
                    p.font.bold = True
                    p.font.color.rgb = WHITE
                else:
                    p.font.color.rgb = DARK
    else:
        _add_bullets(s5, 0.6, 1.35, 12.1, 5.4, "Уникальное предложение", ["В категории пока один товар; используйте карточку как флагманское предложение."])

    # Slide 6: Sales cheat sheet
    s6 = prs.slides.add_slide(blank)
    _add_header(s6, "Шпаргалка менеджера по продажам")
    _add_bullets(
        s6, 0.6, 1.35, 12.1, 5.4,
        "Питч за 30 секунд + условия сделки (ДЕМО-ДАННЫЕ)",
        [
            f"Кому: {product['target_audience'] or 'B2C / B2B клиенты'}",
            f"Главный аргумент: {benefits[0] if benefits else product['name']}",
            "Доставка: Москва/СПб на следующий день (от 15 000 ₽ бесплатно); РФ через СДЭК 2–6 дней (от 20 000 ₽ бесплатно).",
            "Оплата: карты, СБП, рассрочка 0% на 6/12 мес. (от 10 000 ₽), для B2B — счёт + ЭДО.",
            "Когда звать продакт-менеджера: скидка от 10+ шт., тендер 44-ФЗ/223-ФЗ, товар «нет в наличии» или нестандартная совместимость.",
        ],
    )

    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{sku}_sales_deck.pptx"
    out_path = ARTIFACTS_DIR / filename
    prs.save(str(out_path))

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO materials(sku, kind, title, file_path, meta_json, created_at) VALUES(?,?,?,?,?,?)",
            (
                sku,
                "pptx",
                f"Презентация для продаж: {product['name']}",
                str(out_path),
                json.dumps({"slides": len(prs.slides), "alt_sku": alt["sku"] if alt else None}, ensure_ascii=False),
                now_ts(),
            ),
        )

    return {
        "sku": sku,
        "title": product["name"],
        "slides": len(prs.slides),
        "filename": filename,
        "download_url": f"/api/artifacts/{filename}",
    }
