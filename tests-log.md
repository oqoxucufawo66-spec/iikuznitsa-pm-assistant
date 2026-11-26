# Лог сквозного тестирования (tests-log.md)

Все тесты выполнены локально на стенде `uvicorn` с демо-датасетом **«ТехноВитрина (ДЕМО-ДАННЫЕ)»**.

## 1. Старт и индексация базы знаний (JSON + CSV + MD + PDF)
- `GET /health`: `{"status": "ok", "dataset": "ДЕМО-ДАННЫЕ", "provider": "cloudru", "model": "GigaChat/GigaChat-3-Pro", "ru_hosted": true, "heygen_dry_run": true, "products_count": 20, "chunks_count": 26}`
- Загружено товаров: **20** (16 JSON + 4 CSV), пометка датасета: `ДЕМО-ДАННЫЕ`.

## 2. Прогон 10 вопросов через `/api/chat`

### Q1 (Товар из JSON, характеристики)
- **Вопрос:** Какая мощность всасывания, автономность и гарантия у робота-пылесоса AeroClean L10 Pro?
- **Intent:** `product_qa` | **Confidence:** `0.7785` | **Повтор:** `False` (кластер hits=1) | **Эскалация:** `False`
- **Модель / токены / цена:** `cloudru/GigaChat/GigaChat-3-Pro` | `3327+49` ток. | `0.333553 ₽` | `cache_hit=False` | `4243 мс`
- **Источники:** DEMO-VAC-101, DEMO-VAC-102, DEMO-AUD-401, DEMO-LAP-201
- **Шаги агента:** `1_repeat_detector -> 2_planner -> 3_tool_search_kb -> 5_llm_synthesis -> 6_final_answer`
- **Ответ:** Мощность всасывания AeroClean L10 Pro — 6000 Па, автономность до 180 минут, гарантия 24 месяца. [Источник: DEMO-VAC-101]

### Q2 (Товар из CSV, периферия)
- **Вопрос:** Расскажи про клавиатуру TactilePro 75 Wireless: какие переключатели, цена и автономность?
- **Intent:** `product_qa` | **Confidence:** `0.769` | **Повтор:** `False` (кластер hits=1) | **Эскалация:** `False`
- **Модель / токены / цена:** `cloudru/GigaChat/GigaChat-3-Pro` | `2316+91` ток. | `0.248635 ₽` | `cache_hit=False` | `3859 мс`
- **Источники:** DEMO-KEY-901, DEMO-LAP-201
- **Шаги агента:** `1_repeat_detector -> 2_planner -> 3_tool_search_kb -> 5_llm_synthesis -> 6_final_answer`
- **Ответ:** Механическая клавиатура TactilePro 75 Wireless оснащена тихими линейными Hot-Swap переключателями, поддерживает Bluetooth 5.1, 2.4 ГГц и USB-C подключение. Аккумулятор ёмкостью 4000 мА·ч обеспечивает до 200 часов работы без подсветки. Цена — 8990 ₽. [Источник: DEMO-KEY-901]

### Q3 (Сравнение двух товаров)
- **Вопрос:** Сравни ноутбуки NordBook Pro 14 OLED и NordBook Air 15 Office по экрану, памяти и цене.
- **Intent:** `compare` | **Confidence:** `0.7335` | **Повтор:** `False` (кластер hits=1) | **Эскалация:** `False`
- **Модель / токены / цена:** `cloudru/GigaChat/GigaChat-3-Pro` | `2475+266` ток. | `0.314474 ₽` | `cache_hit=False` | `9877 мс`
- **Источники:** DEMO-LAP-202, DEMO-LAP-201
- **Шаги агента:** `1_repeat_detector -> 2_planner -> 3_tool_compare_products -> 5_llm_synthesis -> 6_final_answer`
- **Ответ:** **Сравнение NordBook Pro 14 OLED и NordBook Air 15 Office**  - **Экран:** Pro 14 OLED — 14" OLED 2880×1800, 120 Гц, 400 нит, 100% DCI-P3; Air 15 Office — 15.6" IPS 1920×1080, 60 Гц, 300 нит, матовый [Источник: DEMO-LAP-201, DEMO-LAP-202]. - **Память:** Pro 14 OLED — 32 ГБ LPDDR5X…

### Q4 (FAQ: оплата и доставка)
- **Вопрос:** Можно ли оплатить по СБП или в рассрочку и сколько стоит доставка по Москве?
- **Intent:** `policy_faq` | **Confidence:** `0.57` | **Повтор:** `False` (кластер hits=1) | **Эскалация:** `False`
- **Модель / токены / цена:** `cloudru/GigaChat/GigaChat-3-Pro` | `1916+90` ток. | `0.209946 ₽` | `cache_hit=False` | `4812 мс`
- **Источники:** faq.md, faq.md
- **Шаги агента:** `1_repeat_detector -> 2_planner -> 3_tool_search_kb -> 5_llm_synthesis -> 6_final_answer`
- **Ответ:** Оплата возможна по СБП (QR-код), банковской картой, наличными или картой курьеру, а также в рассрочку 0% на 6 или 12 месяцев от банков-партнёров при заказе от 10 000 ₽. Доставка по Москве курьером — 390 ₽, бесплатно при заказе от 15 000 ₽. [Источник: 1, 2]

### Q5 (PDF-регламент B2B)
- **Вопрос:** Какие ноутбуки протестированы с Astra Linux и за сколько часов отвечают по приоритетной линии B2B Стандарт+?
- **Intent:** `policy_faq` | **Confidence:** `0.6618` | **Повтор:** `False` (кластер hits=1) | **Эскалация:** `False`
- **Модель / токены / цена:** `cloudru/GigaChat/GigaChat-3-Pro` | `2763+86` ток. | `0.290102 ₽` | `cache_hit=False` | `4249 мс`
- **Источники:** b2b_warranty_demo.pdf, DEMO-LAP-201, DEMO-LAP-202
- **Шаги агента:** `1_repeat_detector -> 2_planner -> 3_tool_search_kb -> 5_llm_synthesis -> 6_final_answer`
- **Ответ:** Сертифицированы и протестированы с Astra Linux Special Edition 1.7 и 1.8 ноутбуки NordBook Air 15 Office и NordBook Pro 14 OLED. [Источник: b2b_warranty_demo.pdf]  По приоритетной линии B2B Стандарт+ обращения юридических лиц обрабатываются в течение 4 рабочих часов. [Источник: b…

### Q6 (Повторный вопрос — перефраз)
- **Вопрос:** Какая мощность всасывания и гарантия у AeroClean L10 Pro?
- **Intent:** `product_qa` | **Confidence:** `0.7249` | **Повтор:** `True` (кластер hits=2) | **Эскалация:** `False`
- **Модель / токены / цена:** `cloudru/GigaChat/GigaChat-3-Pro` | `3318+41` ток. | `0.330377 ₽` | `cache_hit=False` | `2233 мс`
- **Источники:** DEMO-VAC-101, DEMO-VAC-102, DEMO-AUD-401, DEMO-LAP-201
- **Шаги агента:** `1_repeat_detector -> 2_planner -> 3_tool_search_kb -> 5_llm_synthesis -> 6_final_answer`
- **Ответ:** Мощность всасывания AeroClean L10 Pro — 6000 Па, гарантия — 24 месяца. [Источник: DEMO-VAC-101]

### Q7 (Точный повтор — проверка кэша LLM)
- **Вопрос:** Какая мощность всасывания, автономность и гарантия у робота-пылесоса AeroClean L10 Pro?
- **Intent:** `product_qa` | **Confidence:** `0.7785` | **Повтор:** `True` (кластер hits=3) | **Эскалация:** `False`
- **Модель / токены / цена:** `cloudru/GigaChat/GigaChat-3-Pro` | `0+0` ток. | `0.0 ₽` | `cache_hit=True` | `547 мс`
- **Источники:** DEMO-VAC-101, DEMO-VAC-102, DEMO-AUD-401, DEMO-LAP-201
- **Шаги агента:** `1_repeat_detector -> 2_planner -> 3_tool_search_kb -> 5_llm_synthesis -> 6_final_answer`
- **Ответ:** Мощность всасывания AeroClean L10 Pro — 6000 Па, автономность до 180 минут, гарантия 24 месяца. [Источник: DEMO-VAC-101]

### Q8 (Эскалация: товар отсутствует)
- **Вопрос:** Когда появится в наличии веб-камера StreamEye 4K Desk?
- **Intent:** `product_qa` | **Confidence:** `0.7599` | **Повтор:** `False` (кластер hits=1) | **Эскалация:** `True` (#PM-1)
- **Модель / токены / цена:** `cloudru/GigaChat/GigaChat-3-Pro` | `0+0` ток. | `0.0 ₽` | `cache_hit=False` | `1315 мс`
- **Источники:** DEMO-CAM-803
- **Шаги агента:** `1_repeat_detector -> 2_planner -> 3_tool_search_kb -> 4_confidence_gate`
- **Ответ:** Не знаю — передаю продакт-менеджеру (обращение #PM-1). По карточке [DEMO-CAM-803] товар сейчас «Нет в наличии», а сроков новой поставки в базе знаний нет.

### Q9 (Эскалация: правило ПМ — тендер/опт)
- **Вопрос:** Нужна корпоративная скидка на 20 ноутбуков NordBook Air 15 под тендер 44-ФЗ.
- **Intent:** `pm_rule_escalation` | **Confidence:** `0.7075` | **Повтор:** `False` (кластер hits=1) | **Эскалация:** `True` (#PM-2)
- **Модель / токены / цена:** `cloudru/GigaChat/GigaChat-3-Pro` | `0+0` ток. | `0.0 ₽` | `cache_hit=False` | `629 мс`
- **Источники:** —
- **Шаги агента:** `1_repeat_detector -> 2_planner -> 3_tool_search_kb -> 4_confidence_gate`
- **Ответ:** Не знаю — передаю продакт-менеджеру (обращение #PM-2). Причина: Требуется согласование индивидуальных условий / тендера (44-ФЗ/223-ФЗ) с продакт-менеджером.

### Q10 (Эскалация: нет в каталоге)
- **Вопрос:** Продаёте ли вы двухдверные холодильники LG с ледогенератором?
- **Intent:** `product_qa` | **Confidence:** `0.1443` | **Повтор:** `False` (кластер hits=1) | **Эскалация:** `True` (#PM-3)
- **Модель / токены / цена:** `cloudru/GigaChat/GigaChat-3-Pro` | `0+0` ток. | `0.0 ₽` | `cache_hit=False` | `936 мс`
- **Источники:** —
- **Шаги агента:** `1_repeat_detector -> 2_planner -> 3_tool_search_kb -> 4_confidence_gate`
- **Ответ:** Не знаю — передаю продакт-менеджеру (обращение #PM-3). В базе знаний (ДЕМО-ДАННЫЕ) не найдено подтверждённой информации для ответа на этот вопрос.

## 3. Генерация материалов и адаптеры
- **PPTX (`POST /api/generate/pptx`):** `DEMO-LAP-201_sales_deck.pptx`, слайдов: **6** (проверено чтением через `python-pptx`).
- **Видеосценарий + HeyGen dry-run (`POST /api/generate/video`):** `/api/artifacts/DEMO-VAC-101_video_script.md`, `/api/artifacts/DEMO-VAC-101_heygen_dry_run.json`, модель `cloudru/GigaChat/GigaChat-3-Pro`, токены `1836+543`, `credits_spent=0`.
- **Telegram-адаптер (`POST /api/telegram/webhook`):** `ok=True`, длина ответа 111 симв.

## 4. Итоговая статистика (`/api/stats` и `/api/escalations`)
- Всего вопросов (включая Telegram): **11**
- Повторяющихся вопросов: **2 (18.2%)**
- Кэш-попаданий LLM: **1**
- Эскалаций продакт-менеджеру: **3** (открыто: 3)
- Суммарно токенов: **18401 + 660**, итого стоимость: **1.957236 ₽**, средняя цена 1 вопроса: **0.177931 ₽**
- Средняя задержка: **3292.4 мс**

Все автопроверки пройдены успешно (`EXIT 0`).