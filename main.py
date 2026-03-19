"""
Telegram-бот для заказа еды в Новогрудке.
Управление только кнопками. Playwright для парсинга меню и оформления заказов.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
import aiohttp
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.async_api import async_playwright

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
PHONE: str = os.getenv("PHONE", "")
ADDRESS: str = os.getenv("ADDRESS", "")
COMMENT: str = os.getenv("COMMENT", "Перезвонить для уточнения заказа")

_raw_ids = os.getenv("ALLOWED_CHAT_IDS", "")
ALLOWED_CHAT_IDS: set[int] = (
    {int(x.strip()) for x in _raw_ids.split(",") if x.strip()}
    if _raw_ids.strip()
    else set()
)

# ---------------------------------------------------------------------------
# MENU CACHE  {key: (timestamp, data)}
# ---------------------------------------------------------------------------

_menu_cache: dict[str, tuple[float, any]] = {}
CACHE_TTL = 3600  # 1 час
_desc_cache: dict[str, str] = {}  # кеш описаний товаров


def _get_cache(key: str):
    entry = _menu_cache.get(key)
    if entry and time.time() - entry[0] < CACHE_TTL:
        return entry[1]
    return None


def _set_cache(key: str, data) -> None:
    _menu_cache[key] = (time.time(), data)


# ---------------------------------------------------------------------------
# ORDER HISTORY
# ---------------------------------------------------------------------------

ORDERS_FILE = "orders_history.json"
MAX_HISTORY = 5


def load_orders_history(chat_id: int) -> list[dict]:
    try:
        with open(ORDERS_FILE) as f:
            data = json.load(f)
        return data.get(str(chat_id), [])
    except Exception:
        return []


def save_order_to_history(chat_id: int, restaurant: str, cart: dict, total: int) -> None:
    try:
        try:
            with open(ORDERS_FILE) as f:
                data = json.load(f)
        except Exception:
            data = {}
        key = str(chat_id)
        history = data.get(key, [])
        history.insert(0, {
            "restaurant": restaurant,
            "items": {k: v.copy() for k, v in cart.items()},
            "total": total,
            "timestamp": datetime.now().strftime("%d.%m %H:%M"),
        })
        data[key] = history[:MAX_HISTORY]
        with open(ORDERS_FILE, "w") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error("Ошибка сохранения истории: %s", e)


RESTAURANTS: dict[str, str] = {
    "Вкусно вместе": "https://vkusnovmeste.by/",
    "Сады Победы": "https://sadypobedy.by/",
}

RESTAURANT_PHONES: dict[str, str] = {
    "Вкусно вместе": "+375 29 349-06-71",
    "Сады Победы": "+375 44 775-37-55",
}

# Категории Садов Победы — стабильные slug-и
SADYPOBEDY_CATEGORIES: dict[str, str] = {
    "Фастфуд": "https://sadypobedy.by/fastfud",
    "Кофе": "https://sadypobedy.by/kofe",
    "Салаты": "https://sadypobedy.by/salaty",
    "Торты": "https://sadypobedy.by/torty",
    "Блины": "https://sadypobedy.by/bliny",
    "Десерты": "https://sadypobedy.by/deserty",
    "Завтрак": "https://sadypobedy.by/zavtrak",
    "Драники": "https://sadypobedy.by/draniki",
}

# ---------------------------------------------------------------------------
# FSM STATES
# ---------------------------------------------------------------------------


class OrderStates(StatesGroup):
    restaurant = State()   # выбор ресторана
    category = State()     # выбор категории
    cart = State()         # просмотр / управление корзиной


# ---------------------------------------------------------------------------
# IN-MEMORY CART  {chat_id: {dish_name: {"qty": int, "price": int}}}
# ---------------------------------------------------------------------------

carts: dict[int, dict[str, dict]] = {}


def get_cart(chat_id: int) -> dict[str, dict]:
    return carts.setdefault(chat_id, {})


def cart_total(cart: dict[str, dict]) -> int:
    return sum(v["qty"] * v["price"] for v in cart.values())


def clear_cart(chat_id: int) -> None:
    carts.pop(chat_id, None)


# ---------------------------------------------------------------------------
# PARSING  (Playwright headless)
# ---------------------------------------------------------------------------


async def parse_categories_vkusnovmeste() -> dict[str, str]:
    """Парсит категории меню с vkusnovmeste.by."""
    cached = _get_cache("vkusnovmeste_cats")
    if cached is not None:
        log.info("vkusnovmeste.by: категории из кеша")
        return cached
    categories: dict[str, str] = {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto("https://vkusnovmeste.by/", timeout=60000)
            await page.wait_for_load_state("networkidle", timeout=20000)

            # Категории которые не нужны отцу
            skip = {"Роллы", "Сеты"}

            links = await page.query_selector_all("ul.hmenu li a")
            for link in links:
                text = (await link.inner_text()).strip()
                href = await link.get_attribute("href")
                if text and href and text not in skip:
                    # Убираем якорь #wbs1
                    clean = href.split("#")[0]
                    full_url = clean if clean.startswith("http") else f"https://vkusnovmeste.by{clean}"
                    categories[text] = full_url

            log.info("vkusnovmeste.by: найдено категорий: %d", len(categories))
        except Exception as e:
            log.error("Ошибка парсинга категорий vkusnovmeste.by: %s", e)
        finally:
            await browser.close()
    if categories:
        _set_cache("vkusnovmeste_cats", categories)
    return categories


async def parse_dishes(url: str, site: str) -> list[dict]:
    """
    Парсит список блюд (название + цена) со страницы категории.
    Возвращает [{"name": str, "price": int}, ...]
    """
    key = f"dishes_{url}"
    cached = _get_cache(key)
    if cached is not None:
        log.info("dishes: из кеша %s", url)
        return cached
    dishes: list[dict] = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(url, timeout=30000)
            await page.wait_for_timeout(4000)

            if "sadypobedy" in site:
                dishes = await _parse_dishes_sadypobedy(page)
            else:
                dishes = await _parse_dishes_vkusnovmeste(page)

            log.info("Блюд на %s: %d", url, len(dishes))
        except Exception as e:
            log.error("Ошибка парсинга блюд %s: %s", url, e)
        finally:
            await browser.close()
    if dishes:
        _set_cache(key, dishes)
    return dishes


async def _parse_dishes_vkusnovmeste(page) -> list[dict]:
    dishes: list[dict] = []
    await page.wait_for_load_state("networkidle", timeout=15000)

    items = await page.query_selector_all(".wb-store-item")
    for item in items:
        try:
            name_el = await item.query_selector(".wb-store-name")
            price_el = await item.query_selector(".wb-store-price")
            if not name_el or not price_el:
                continue
            name = (await name_el.inner_text()).strip()
            price_text = (await price_el.inner_text()).strip()
            price = _extract_price(price_text)

            product_url = None
            link_el = await name_el.query_selector("a")
            if link_el:
                href = await link_el.get_attribute("href")
                if href:
                    clean = href.split("#")[0]
                    product_url = clean if clean.startswith("http") else f"https://vkusnovmeste.by{clean}"

            image_url = None
            img_el = await item.query_selector(".wb-store-thumb img")
            if img_el:
                src = await img_el.get_attribute("src")
                if src:
                    image_url = src if src.startswith("http") else f"https://vkusnovmeste.by/{src.lstrip('/')}"

            if name and price > 0:
                dishes.append({"name": name, "price": price, "image_url": image_url, "product_url": product_url})
        except Exception:
            continue
    return dishes


async def _parse_dishes_sadypobedy(page) -> list[dict]:
    dishes: list[dict] = []
    await page.wait_for_timeout(5000)

    cards = await page.query_selector_all(".t-store__card")
    for card in cards:
        try:
            name_el = await card.query_selector(".t-name")
            price_el = await card.query_selector(".t-store__card__price-value")
            if not name_el or not price_el:
                continue
            name = (await name_el.inner_text()).strip()
            # Подзаголовок (например MINI)
            descr_el = await card.query_selector(".t-store__card__descr")
            subtitle = ""
            if descr_el:
                subtitle = (await descr_el.inner_text()).strip()
                if subtitle:
                    name = f"{name} ({subtitle})"
            price_text = (await price_el.inner_text()).strip()
            price = _extract_price(price_text)

            image_url = None
            img_el = await card.query_selector(".js-product-img")
            if img_el:
                image_url = await img_el.get_attribute("data-original")

            if name and price > 0:
                dishes.append({"name": name, "price": price, "image_url": image_url, "product_url": None})
        except Exception:
            continue

    return dishes


def _extract_price(text: str) -> int:
    """Извлекает целое число из строки цены. '5.50 руб.' → 550, '550' → 550."""
    import re
    text = text.replace("\xa0", " ").replace(",", ".")
    match = re.search(r"(\d+(?:\.\d+)?)", text)
    if not match:
        return 0
    val = float(match.group(1))
    # Если цена < 100 — скорее всего рубли с копейками, умножаем
    if val < 100:
        return int(val * 100)
    return int(val)


async def fetch_product_description(product_url: str) -> str:
    """Загружает страницу товара через Playwright и извлекает состав/описание."""
    if product_url in _desc_cache:
        return _desc_cache[product_url]
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(product_url, timeout=15000)
            await page.wait_for_load_state("networkidle", timeout=8000)
            import re as _re
            page_text = await page.evaluate("() => document.body.innerText")
            # Ищем текст после слова "Описание"
            match = _re.search(r'Описание\s*\n+\s*(.+?)(?:\n|$)', page_text, _re.IGNORECASE)
            description = match.group(1).strip() if match else ""
            await browser.close()
        if description:
            log.info("fetch_product_description: '%s'", description[:80])
            _desc_cache[product_url] = description
        else:
            log.info("fetch_product_description: описание не найдено на %s", product_url)
            _desc_cache[product_url] = ""
        return description
    except Exception as e:
        log.warning("fetch_product_description %s: %s", product_url, e)
        return ""


# ---------------------------------------------------------------------------
# ORDER AUTOMATION (Playwright)
# ---------------------------------------------------------------------------


async def place_order(chat_id: int, restaurant_name: str) -> Optional[bytes]:
    """
    Оформляет заказ через Playwright.
    Возвращает bytes скриншота или None при ошибке.
    """
    cart = get_cart(chat_id)
    if not cart:
        return None

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            if "Вкусно" in restaurant_name:
                screenshot = await _order_vkusnovmeste(browser, cart)
            else:
                screenshot = await _order_sadypobedy(browser, cart)
            return screenshot
        except Exception as e:
            log.error("Ошибка оформления заказа (%s): %s", restaurant_name, e)
            return None
        finally:
            await browser.close()


async def _order_vkusnovmeste(browser, cart: dict) -> bytes:
    page = await browser.new_page()
    await page.goto("https://vkusnovmeste.by/", timeout=30000)
    await page.wait_for_timeout(2000)

    for dish_name in cart:
        qty = cart[dish_name]["qty"]
        for _ in range(qty):
            try:
                # Ищем блюдо по тексту
                el = page.get_by_text(dish_name, exact=False).first
                await el.scroll_into_view_if_needed()
                await page.wait_for_timeout(500)

                # Кнопка «Добавить» рядом с блюдом
                parent = await el.evaluate_handle("el => el.closest('.product, .dish, article, .item') || el.parentElement")
                btn = await parent.query_selector("button")
                if btn:
                    await btn.click()
                else:
                    await page.get_by_role("button", name="Добавить").first.click()
                await page.wait_for_timeout(1000)
            except Exception as e:
                log.warning("Не удалось добавить '%s': %s", dish_name, e)

    # Переход в корзину
    try:
        cart_btn = page.get_by_role("link", name="Корзина")
        if not await cart_btn.count():
            cart_btn = page.get_by_text("Корзина").first
        await cart_btn.click()
        await page.wait_for_timeout(2000)
    except Exception as e:
        log.warning("Не удалось перейти в корзину: %s", e)

    # Заполнение формы
    await _fill_order_form(page)

    screenshot = await page.screenshot(full_page=True)
    return screenshot


async def _order_sadypobedy(browser, cart: dict) -> bytes:
    page = await browser.new_page()

    # Добавляем каждое блюдо с его страницы категории
    for dish_name in cart:
        qty = cart[dish_name]["qty"]
        # Ищем блюдо через поиск или категорию (упрощённо — идём на главную)
        await page.goto("https://sadypobedy.by/", timeout=30000)
        await page.wait_for_timeout(3000)

        for _ in range(qty):
            try:
                el = page.get_by_text(dish_name, exact=False).first
                await el.scroll_into_view_if_needed()
                await page.wait_for_timeout(500)

                parent = await el.evaluate_handle("el => el.closest('.t-store__col, .t-store__card') || el.parentElement")
                btn = await parent.query_selector("a.t-store__cart-btn, button")
                if btn:
                    await btn.click()
                else:
                    await page.get_by_role("button", name="В корзину").first.click()
                await page.wait_for_timeout(1000)
            except Exception as e:
                log.warning("Не удалось добавить '%s': %s", dish_name, e)

    # Переход в корзину Tilda
    try:
        await page.get_by_text("Корзина").first.click()
        await page.wait_for_timeout(2000)
    except Exception as e:
        log.warning("Переход в корзину: %s", e)

    await _fill_order_form(page)

    screenshot = await page.screenshot(full_page=True)
    return screenshot


async def _fill_order_form(page) -> None:
    """Заполняет поля формы заказа фиксированными данными."""
    await page.wait_for_timeout(1000)

    field_map = [
        (["телефон", "phone", "tel"], PHONE),
        (["адрес", "address", "улица"], ADDRESS),
        (["комментарий", "comment", "примечание", "пожелание"], COMMENT),
    ]

    for keywords, value in field_map:
        for kw in keywords:
            try:
                inputs = await page.query_selector_all(f"input[placeholder*='{kw}' i], textarea[placeholder*='{kw}' i], input[name*='{kw}' i]")
                for inp in inputs:
                    if await inp.is_visible():
                        await inp.fill(value)
                        await page.wait_for_timeout(300)
                        break
            except Exception:
                pass

    # Оплата наличными
    try:
        cash_options = [
            page.get_by_label("Наличными"),
            page.get_by_text("Наличными курьеру"),
            page.get_by_role("radio", name="Наличными"),
        ]
        for opt in cash_options:
            if await opt.count():
                await opt.first.click()
                break
    except Exception:
        pass

    await page.wait_for_timeout(500)

    # Кнопка отправки заказа
    submit_options = [
        page.get_by_role("button", name="Оформить заказ"),
        page.get_by_role("button", name="Заказать"),
        page.get_by_role("button", name="Подтвердить"),
        page.get_by_text("Оформить заказ"),
    ]
    for btn in submit_options:
        try:
            if await btn.count():
                await btn.first.click()
                await page.wait_for_timeout(3000)
                break
        except Exception:
            pass


# ---------------------------------------------------------------------------
# KEYBOARDS
# ---------------------------------------------------------------------------


def build_restaurant_keyboard(has_history: bool = False) -> ReplyKeyboardMarkup:
    buttons = [[KeyboardButton(text=name)] for name in RESTAURANTS]
    if has_history:
        buttons.append([KeyboardButton(text="📋 Повторить заказ")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)


def build_nav_keyboard() -> ReplyKeyboardMarkup:
    """Постоянная нижняя панель навигации."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="🏠 Сменить ресторан"),
                KeyboardButton(text="🛒 Моя корзина"),
            ],
            [
                KeyboardButton(text="🔄 Начать заново"),
            ],
        ],
        resize_keyboard=True,
    )


def build_categories_keyboard(categories: dict[str, str]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text=name, callback_data=f"cat|{i}")]
        for i, name in enumerate(categories)
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_dishes_keyboard(dishes: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for i, dish in enumerate(dishes[:40]):  # Telegram лимит кнопок
        label = f"🍽 {dish['name']}  |  {dish['price']//100}.{dish['price']%100:02d} р."
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"dish|{i}")])
    buttons.append([InlineKeyboardButton(text="🛒 Корзина", callback_data="show_cart")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_cat")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_cart_keyboard(cart: dict) -> InlineKeyboardMarkup:
    buttons = []
    for i, dish_name in enumerate(cart):
        qty = cart[dish_name]["qty"]
        buttons.append([
            InlineKeyboardButton(text=f"➖ {dish_name[:20]}", callback_data=f"dec|{i}"),
            InlineKeyboardButton(text=str(qty), callback_data="noop"),
            InlineKeyboardButton(text="➕", callback_data=f"inc|{i}"),
        ])
    buttons.append([InlineKeyboardButton(text="🗑 Очистить", callback_data="clear")])
    buttons.append([InlineKeyboardButton(text="✅ Оформить заказ", callback_data="order_confirm")])
    buttons.append([InlineKeyboardButton(text="⬅️ К меню", callback_data="back_dishes")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def cart_text(cart: dict) -> str:
    if not cart:
        return "Корзина пуста."
    lines = ["🛒 *Ваша корзина:*\n"]
    for dish_name, info in cart.items():
        total = info["qty"] * info["price"]
        lines.append(f"• {dish_name} × {info['qty']} = {total//100}.{total%100:02d} руб.")
    total = cart_total(cart)
    lines.append(f"\n*Итого: {total//100}.{total%100:02d} руб.*")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# ROUTER & HANDLERS
# ---------------------------------------------------------------------------

router = Router()


def is_allowed(chat_id: int) -> bool:
    if not ALLOWED_CHAT_IDS:
        return True
    return chat_id in ALLOWED_CHAT_IDS


async def check_allowed(message_or_callback) -> bool:
    """Проверяет доступ и отвечает если запрещён."""
    if not ALLOWED_CHAT_IDS:
        return True
    chat_id = (
        message_or_callback.chat.id
        if hasattr(message_or_callback, "chat")
        else message_or_callback.message.chat.id
    )
    if chat_id not in ALLOWED_CHAT_IDS:
        text = "⛔ Бот работает в приватном режиме. Обратитесь к владельцу."
        if hasattr(message_or_callback, "answer"):
            await message_or_callback.answer(text)
        else:
            await message_or_callback.answer(text, show_alert=True)
        return False
    return True


async def loading_animation(msg: Message, base: str, task: asyncio.Task) -> None:
    """Анимирует сообщение пока task не завершён."""
    dots = ["", ".", "..", "..."]
    i = 0
    while not task.done():
        try:
            await msg.edit_text(f"⏳ {base}{dots[i % 4]}")
        except Exception:
            pass
        i += 1
        await asyncio.sleep(1.5)


@router.message(Command("start"))
@router.message(Command("restart"))
@router.message(F.text == "start")
@router.message(F.text == "Start")
async def cmd_start(message: Message, state: FSMContext) -> None:
    if not await check_allowed(message):
        return
    clear_cart(message.chat.id)
    await state.clear()
    await state.set_state(OrderStates.restaurant)
    history = load_orders_history(message.chat.id)
    await message.answer(
        "Привет! Выберите ресторан:",
        reply_markup=build_restaurant_keyboard(has_history=bool(history)),
    )
    log.info("chat=%d /start", message.chat.id)


@router.message(F.text == "🔄 Начать заново")
async def handle_nav_restart(message: Message, state: FSMContext) -> None:
    if not await check_allowed(message):
        return
    clear_cart(message.chat.id)
    await state.clear()
    await state.set_state(OrderStates.restaurant)
    history = load_orders_history(message.chat.id)
    await message.answer("Выберите ресторан:", reply_markup=build_restaurant_keyboard(has_history=bool(history)))


@router.message(F.text == "🏠 Сменить ресторан")
async def handle_nav_home(message: Message, state: FSMContext) -> None:
    if not await check_allowed(message):
        return
    await state.clear()
    await state.set_state(OrderStates.restaurant)
    history = load_orders_history(message.chat.id)
    await message.answer("Выберите ресторан:", reply_markup=build_restaurant_keyboard(has_history=bool(history)))


@router.message(F.text == "🛒 Моя корзина")
async def handle_nav_cart(message: Message, state: FSMContext) -> None:
    if not await check_allowed(message):
        return
    chat_id = message.chat.id
    cart = get_cart(chat_id)
    await state.set_state(OrderStates.cart)
    if not cart:
        await message.answer("Корзина пуста. Добавьте блюда.")
        return
    await message.answer(
        cart_text(cart),
        reply_markup=build_cart_keyboard(cart),
        parse_mode="Markdown",
    )


@router.message(OrderStates.restaurant)
async def handle_restaurant(message: Message, state: FSMContext) -> None:
    if not await check_allowed(message):
        return
    name = message.text or ""
    if name not in RESTAURANTS:
        await message.answer("Пожалуйста, выберите ресторан кнопкой.")
        return

    await state.update_data(restaurant=name)
    await state.set_state(OrderStates.category)

    status_msg = await message.answer("⏳ Загружаю меню...", reply_markup=build_nav_keyboard())

    # Загрузка категорий
    if "Вкусно" in name:
        task = asyncio.create_task(parse_categories_vkusnovmeste())
        await loading_animation(status_msg, "Загружаю меню", task)
        categories = await task
        if not categories:
            categories = {"Меню": RESTAURANTS[name]}  # fallback
    else:
        categories = SADYPOBEDY_CATEGORIES

    try:
        await status_msg.edit_text("✅ Меню загружено!")
    except Exception:
        pass

    await state.update_data(categories=categories)
    cat_list = list(categories.keys())
    await state.update_data(cat_list=cat_list)

    await message.answer(
        f"*{name}* — выберите категорию:",
        reply_markup=build_categories_keyboard(categories),
        parse_mode="Markdown",
    )
    log.info("chat=%d выбрал ресторан: %s", message.chat.id, name)


@router.callback_query(F.data.startswith("cat|"))
async def handle_category(callback: CallbackQuery, state: FSMContext) -> None:
    if not await check_allowed(callback):
        return
    idx = int(callback.data.split("|")[1])
    data = await state.get_data()
    cat_list: list[str] = data.get("cat_list", [])
    categories: dict[str, str] = data.get("categories", {})

    if idx >= len(cat_list):
        await callback.answer("Ошибка")
        return

    cat_name = cat_list[idx]
    cat_url = categories[cat_name]
    restaurant: str = data.get("restaurant", "")

    await callback.message.edit_text(f"⏳ Загружаю «{cat_name}»...")

    site = "sadypobedy" if "Сады" in restaurant else "vkusnovmeste"
    task = asyncio.create_task(parse_dishes(cat_url, site))
    await loading_animation(callback.message, f"Загружаю «{cat_name}»", task)
    dishes = await task

    if not dishes:
        await callback.message.edit_text(
            "Не удалось загрузить блюда. Попробуйте другую категорию.",
            reply_markup=build_categories_keyboard(categories),
        )
        await callback.answer()
        return

    await state.update_data(dishes=dishes, current_category=cat_name, dishes_msg_id=callback.message.message_id)

    await callback.message.edit_text(
        f"*{cat_name}* — выберите блюдо:",
        reply_markup=build_dishes_keyboard(dishes),
        parse_mode="Markdown",
    )
    await callback.answer()
    log.info("chat=%d категория: %s, блюд: %d", callback.message.chat.id, cat_name, len(dishes))


@router.callback_query(F.data.startswith("dish|"))
async def handle_dish_detail(callback: CallbackQuery, state: FSMContext) -> None:
    """Показывает карточку блюда с фото, описанием и кнопкой добавить."""
    if not await check_allowed(callback):
        return
    idx = int(callback.data.split("|")[1])
    data = await state.get_data()
    dishes: list[dict] = data.get("dishes", [])

    if idx >= len(dishes):
        await callback.answer("Ошибка")
        return

    dish = dishes[idx]
    price_str = f"{dish['price']//100}.{dish['price']%100:02d} р."
    # Загружаем описание с страницы товара
    product_url = dish.get("product_url")
    log.info("dish detail: product_url=%s", product_url)
    description = ""
    if product_url:
        description = await fetch_product_description(product_url)
        log.info("dish detail: description='%s'", description[:80] if description else "(пусто)")
    caption = f"*{dish['name']}*\n💰 {price_str}"
    if description:
        caption += f"\n\n_{description}_"

    detail_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить в корзину", callback_data=f"add|{idx}")],
        [InlineKeyboardButton(text="⬅️ К списку блюд", callback_data="back_dishes")],
    ])

    await callback.answer()
    # Удаляем список блюд, чтобы карточка появилась на его месте
    try:
        await callback.message.delete()
    except Exception:
        pass
    image_url = dish.get("image_url")
    if image_url:
        try:
            msg = await callback.message.answer_photo(
                photo=image_url,
                caption=caption,
                reply_markup=detail_kb,
                parse_mode="Markdown",
            )
            await state.update_data(detail_msg_id=msg.message_id)
            return
        except Exception:
            pass
    # Без фото — просто текст
    msg = await callback.message.answer(
        caption,
        reply_markup=detail_kb,
        parse_mode="Markdown",
    )
    await state.update_data(detail_msg_id=msg.message_id)


@router.callback_query(F.data.startswith("add|"))
async def handle_add_dish(callback: CallbackQuery, state: FSMContext) -> None:
    if not await check_allowed(callback):
        return
    idx = int(callback.data.split("|")[1])
    data = await state.get_data()
    dishes: list[dict] = data.get("dishes", [])

    if idx >= len(dishes):
        await callback.answer("Ошибка")
        return

    dish = dishes[idx]
    chat_id = callback.message.chat.id
    cart = get_cart(chat_id)

    if dish["name"] in cart:
        cart[dish["name"]]["qty"] += 1
    else:
        cart[dish["name"]] = {"qty": 1, "price": dish["price"]}

    await callback.answer(f"✅ {dish['name']} добавлено в корзину")
    log.info("chat=%d добавил: %s", chat_id, dish["name"])


@router.callback_query(F.data == "show_cart")
async def handle_show_cart(callback: CallbackQuery, state: FSMContext) -> None:
    if not await check_allowed(callback):
        return
    chat_id = callback.message.chat.id
    cart = get_cart(chat_id)
    await state.set_state(OrderStates.cart)

    if not cart:
        await callback.message.edit_text("Корзина пуста. Добавьте блюда.")
        await callback.answer()
        return

    await callback.message.edit_text(
        cart_text(cart),
        reply_markup=build_cart_keyboard(cart),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.callback_query(F.data.startswith("inc|"))
async def handle_inc(callback: CallbackQuery, state: FSMContext) -> None:
    chat_id = callback.message.chat.id
    if not await check_allowed(callback):
        return
    idx = int(callback.data.split("|")[1])
    cart = get_cart(chat_id)
    keys = list(cart.keys())
    if idx < len(keys):
        cart[keys[idx]]["qty"] += 1
    await _refresh_cart(callback, cart)


@router.callback_query(F.data.startswith("dec|"))
async def handle_dec(callback: CallbackQuery, state: FSMContext) -> None:
    chat_id = callback.message.chat.id
    if not await check_allowed(callback):
        return
    idx = int(callback.data.split("|")[1])
    cart = get_cart(chat_id)
    keys = list(cart.keys())
    if idx < len(keys):
        name = keys[idx]
        cart[name]["qty"] -= 1
        if cart[name]["qty"] <= 0:
            del cart[name]
    await _refresh_cart(callback, cart)


async def _refresh_cart(callback: CallbackQuery, cart: dict) -> None:
    if not cart:
        await callback.message.edit_text("Корзина пуста.")
        await callback.answer()
        return
    await callback.message.edit_text(
        cart_text(cart),
        reply_markup=build_cart_keyboard(cart),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.callback_query(F.data == "clear")
async def handle_clear(callback: CallbackQuery, state: FSMContext) -> None:
    chat_id = callback.message.chat.id
    if not await check_allowed(callback):
        return
    clear_cart(chat_id)
    await callback.message.edit_text("Корзина очищена.")
    await callback.answer()
    log.info("chat=%d очистил корзину", chat_id)


@router.callback_query(F.data == "order_confirm")
async def handle_order_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    """Показывает экран подтверждения перед оформлением."""
    chat_id = callback.message.chat.id
    if not await check_allowed(callback):
        return
    cart = get_cart(chat_id)
    if not cart:
        await callback.answer("Корзина пуста!")
        return

    data = await state.get_data()
    restaurant: str = data.get("restaurant", "")
    total = cart_total(cart)

    lines = [f"📋 *Подтвердите заказ в «{restaurant}»:*\n"]
    for dish_name, info in cart.items():
        subtotal = info["qty"] * info["price"]
        lines.append(f"• {dish_name} × {info['qty']} = {subtotal//100}.{subtotal%100:02d} р.")
    lines.append(f"\n💰 *Итого: {total//100}.{total%100:02d} р.*")
    lines.append(f"📍 Адрес: {ADDRESS}")
    lines.append(f"💳 Оплата: наличными курьеру")

    confirm_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, оформить!", callback_data="order")],
        [InlineKeyboardButton(text="✏️ Изменить корзину", callback_data="show_cart")],
    ])

    await callback.message.edit_text(
        "\n".join(lines),
        reply_markup=confirm_kb,
        parse_mode="Markdown",
    )
    await callback.answer()


@router.callback_query(F.data == "order")
async def handle_order(callback: CallbackQuery, state: FSMContext) -> None:
    chat_id = callback.message.chat.id
    if not await check_allowed(callback):
        return
    data = await state.get_data()
    restaurant: str = data.get("restaurant", "")
    cart = get_cart(chat_id)

    if not cart:
        await callback.answer("Корзина пуста!")
        return

    await callback.message.edit_text("⏳ Оформляю заказ, подождите 1–2 минуты...")
    await callback.answer()

    log.info("chat=%d оформляет заказ в %s", chat_id, restaurant)
    screenshot = await place_order(chat_id, restaurant)

    if screenshot:
        save_order_to_history(chat_id, restaurant, cart, cart_total(cart))
        await callback.message.answer_photo(
            photo=screenshot,
            caption="✅ Заказ оформлен! Скриншот подтверждения.",
        )
        clear_cart(chat_id)
        await state.clear()
        await state.set_state(OrderStates.restaurant)
        await callback.message.answer(
            "Хотите сделать ещё один заказ?",
            reply_markup=build_restaurant_keyboard(has_history=True),
        )
    else:
        phone = RESTAURANT_PHONES.get(restaurant, "")
        phone_line = f"\n📞 Позвоните сами: {phone}" if phone else ""
        await callback.message.answer(
            f"❌ Не удалось оформить заказ автоматически.{phone_line}\n"
            "Попробуйте позже или закажите по телефону."
        )
    log.info("chat=%d заказ завершён, успех=%s", chat_id, screenshot is not None)


@router.callback_query(F.data == "back_cat")
async def handle_back_cat(callback: CallbackQuery, state: FSMContext) -> None:
    if not await check_allowed(callback):
        return
    data = await state.get_data()
    categories: dict[str, str] = data.get("categories", {})
    restaurant: str = data.get("restaurant", "Ресторан")
    await state.set_state(OrderStates.category)
    await callback.message.edit_text(
        f"*{restaurant}* — выберите категорию:",
        reply_markup=build_categories_keyboard(categories),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.callback_query(F.data == "back_dishes")
async def handle_back_dishes(callback: CallbackQuery, state: FSMContext) -> None:
    if not await check_allowed(callback):
        return
    data = await state.get_data()
    dishes: list[dict] = data.get("dishes", [])
    cat_name: str = data.get("current_category", "Блюда")
    await state.set_state(OrderStates.category)
    # Удаляем карточку блюда (или сообщение корзины)
    try:
        await callback.message.delete()
    except Exception:
        pass
    if dishes:
        await callback.message.answer(
            f"*{cat_name}* — выберите блюдо:",
            reply_markup=build_dishes_keyboard(dishes),
            parse_mode="Markdown",
        )
    else:
        categories = data.get("categories", {})
        restaurant = data.get("restaurant", "Ресторан")
        await callback.message.answer(
            f"*{restaurant}* — выберите категорию:",
            reply_markup=build_categories_keyboard(categories),
            parse_mode="Markdown",
        )
    await callback.answer()


@router.message(F.text == "📋 Повторить заказ")
async def handle_repeat_order(message: Message, state: FSMContext) -> None:
    if not await check_allowed(message):
        return
    history = load_orders_history(message.chat.id)
    if not history:
        await message.answer("История заказов пуста.")
        return
    buttons = []
    for i, order in enumerate(history):
        total_str = f"{order['total']//100}.{order['total']%100:02d}"
        label = f"{order['restaurant']} — {total_str}р. ({order['timestamp']})"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"repeat|{i}")])
    await message.answer(
        "Выберите заказ для повторения:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("repeat|"))
async def handle_repeat_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    if not await check_allowed(callback):
        return
    idx = int(callback.data.split("|")[1])
    history = load_orders_history(callback.message.chat.id)
    if idx >= len(history):
        await callback.answer("Заказ не найден")
        return
    order = history[idx]
    chat_id = callback.message.chat.id
    carts[chat_id] = {k: v.copy() for k, v in order["items"].items()}
    await state.update_data(restaurant=order["restaurant"])
    await state.set_state(OrderStates.cart)
    cart = get_cart(chat_id)
    await callback.message.edit_text(
        f"Корзина восстановлена из заказа от {order['timestamp']}:\n\n" + cart_text(cart),
        reply_markup=build_cart_keyboard(cart),
        parse_mode="Markdown",
    )
    await callback.answer()


@router.callback_query(F.data == "noop")
async def handle_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.message()
async def handle_any_message(message: Message, state: FSMContext) -> None:
    """Если пользователь написал что-то при пустом чате — запускаем бота."""
    if not await check_allowed(message):
        return
    current = await state.get_state()
    if current is None:
        clear_cart(message.chat.id)
        await state.set_state(OrderStates.restaurant)
        history = load_orders_history(message.chat.id)
        await message.answer(
            "Выберите ресторан:",
            reply_markup=build_restaurant_keyboard(has_history=bool(history)),
        )


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------


async def main() -> None:
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не задан в .env")
    if not ALLOWED_CHAT_IDS:
        log.warning("⚠️  ALLOWED_CHAT_IDS пустой — бот доступен ВСЕМ пользователям!")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    from aiogram.types import BotCommand
    await bot.set_my_commands([
        BotCommand(command="start", description="Начать заказ"),
        BotCommand(command="restart", description="Начать заново"),
    ])

    log.info("Бот запущен.")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
