import os
import asyncio
import aiosqlite
from datetime import datetime, timedelta
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder, InlineKeyboardBuilder
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

# Для календаря используем готовую библиотеку (или напиши свой)
from aiogram_calendar import SimpleCalendar, SimpleCalendarCallback

# --- 1. ЗАГРУЗКА НАСТРОЕК ---
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]
PORTFOLIO_URL = os.getenv("PORTFOLIO_URL")
MIN_DAYS_FOR_ORDER = int(os.getenv("MIN_DAYS_FOR_ORDER", 3))
MIN_DAYS_TO_CANCEL = int(os.getenv("MIN_DAYS_TO_CANCEL", 1))
MAX_ORDERS_PER_DAY = int(os.getenv("MAX_ORDERS_PER_DAY", 5))
REMINDER_HOURS_BEFORE = int(os.getenv("REMINDER_HOURS_BEFORE", 24))

DB_NAME = "pastry_bot.db"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()

# --- 2. БАЗА ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        # Таблица пользователей
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT
            )
        """)
        # Таблица заказов (Добавили STATUS с дефолтным значением)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                target_date TEXT,
                category TEXT,
                weight TEXT,
                ref_photo TEXT,
                comment TEXT,
                status TEXT DEFAULT 'pending'
            )
        """)
        # Таблица прайс-листа
        await db.execute("""
            CREATE TABLE IF NOT EXISTS price_list (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT,
                name TEXT,
                price INTEGER
            )
        """)
        await db.commit()

# --- 3. МАШИНА СОСТОЯНИЙ (FSM) ---
class OrderFSM(StatesGroup):
    waiting_for_date = State()
    waiting_for_category = State()
    waiting_for_product = State()
    waiting_for_weight = State()
    waiting_for_phone = State()
    waiting_for_photo = State()
    waiting_for_comment = State()

class AdminPriceFSM(StatesGroup):
    confirm_delete = State()
    waiting_for_category = State()
    waiting_for_name = State()
    waiting_for_price = State()

class BroadcastFSM(StatesGroup):
    waiting_for_message = State()

# --- 4. КЛАВИАТУРЫ ---
def get_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🍰 Портфолио", url=PORTFOLIO_URL)],
        [InlineKeyboardButton(text="📋 Прайс-лист", callback_data="price_list")],
        [InlineKeyboardButton(text="🛒 Сделать заказ", callback_data="make_order")],
        [InlineKeyboardButton(text="❌ Отменить заказ", callback_data="cancel_order")],
    ])

def get_admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Активные заказы", callback_data="admin_orders")],
        [InlineKeyboardButton(text="Изменить прайс", callback_data="admin_price")],
        [InlineKeyboardButton(text="Рассылка", callback_data="admin_broadcast")]
    ])

# --- 5. ОСНОВНАЯ ЛОГИКА ---
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (message.from_user.id,))
        await db.commit()
    await state.clear()
    await message.answer(
        "Добро пожаловать в кондитерскую! 🎂\nВыберите нужное действие ниже:",
        reply_markup=get_main_kb()
    )

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id in ADMIN_IDS:
        await message.answer("🛠 Админ-панель:", reply_markup=get_admin_kb())

@router.callback_query(F.data == "price_list")
async def show_price(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT category, name, price FROM price_list") as cursor:
            prices = await cursor.fetchall()
    
    if not prices:
        await callback.message.answer("Прайс-лист пока пуст. Загляните позже!")
        await callback.answer()
        return

    text = "📖 <b>Наш прайс-лист:</b>\n\n"
    current_cat = ""
    for cat, name, price in prices:
        if cat != current_cat:
            text += f"\n🔹 <b>{cat}</b>\n"
            current_cat = cat
        text += f" — {name}: {price} руб.\n"
    
    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()

# --- 6. ОФОРМЛЕНИЕ ЗАКАЗА ---
@router.callback_query(F.data == "make_order")
async def process_make_order(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "Выберите дату готовности заказа:",
        reply_markup=await SimpleCalendar().start_calendar()
    )
    await state.set_state(OrderFSM.waiting_for_date)

# Обработка выбора в календаре
@router.callback_query(SimpleCalendarCallback.filter(), OrderFSM.waiting_for_date)
async def process_calendar(callback: CallbackQuery, callback_data: SimpleCalendarCallback, state: FSMContext):
    selected, date = await SimpleCalendar().process_selection(callback, callback_data)
    if selected:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        date_str = date.strftime("%Y-%m-%d") # Превращаем в строку для БД
        
        days_diff = (date - today).days
        
        # Проверка лимита (используем строку даты)
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT COUNT(*) FROM orders WHERE target_date = ? AND status != 'cancelled'", (date_str,)) as cursor:
                count = await cursor.fetchone()
                if count[0] >= MAX_ORDERS_PER_DAY:
                    await callback.message.answer(f"⚠️ На {date_str} мест нет (макс. {MAX_ORDERS_PER_DAY}). Выберите другую дату:")
                    return

        if days_diff < MIN_DAYS_FOR_ORDER:
            await callback.message.answer(
                f"⚠️ Заказ можно оформить минимум за {MIN_DAYS_FOR_ORDER} дня.\n"
                f"Ближайшая доступная дата: {(today + timedelta(days=MIN_DAYS_FOR_ORDER)).strftime('%d.%m.%Y')}\n"
                f"Выберите другую дату:",
                reply_markup=await SimpleCalendar().start_calendar()
            )
            await callback.answer()
            return

        await state.update_data(target_date=date.strftime("%Y-%m-%d"))
        
        # Далее идет код выбора категорий из БД (как мы писали ранее)
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT DISTINCT category FROM price_list") as cursor:
                categories = await cursor.fetchall()
        
        if not categories:
            await callback.message.answer("Прайс-лист пуст.")
            await state.clear()
            return

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=cat[0], callback_data=f"cat_{cat[0]}")] for cat in categories
        ])
        await callback.message.edit_text("Выберите категорию изделия:", reply_markup=kb)
        await state.set_state(OrderFSM.waiting_for_category)

@router.callback_query(F.data.startswith("cat_"), OrderFSM.waiting_for_category)
async def process_category(callback: CallbackQuery, state: FSMContext):
    category = callback.data.split("_")[1]
    await state.update_data(category=category)
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT name, price FROM price_list WHERE category = ?", (category,)) as cursor:
            products = await cursor.fetchall()

    if not products:
        await callback.answer("В этой категории пока нет товаров.", show_alert=True)
        return

    # Создаем кнопки с товарами и ценами
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{name} ({price} руб.)", callback_data=f"prod_{name}")] 
        for name, price in products
    ])
    
    await callback.message.edit_text(f"Вы выбрали '{category}'. Теперь выберите конкретное изделие:", reply_markup=kb)
    await state.set_state(OrderFSM.waiting_for_product)

@router.callback_query(F.data.startswith("prod_"), OrderFSM.waiting_for_product)
async def process_product(callback: CallbackQuery, state: FSMContext):
    product_name = callback.data.replace("prod_", "")
    await state.update_data(product_name=product_name)
    
    await callback.message.answer(f"Вы выбрали {product_name}. Напишите желаемый вес или количество:")
    await state.set_state(OrderFSM.waiting_for_weight)

@router.message(OrderFSM.waiting_for_weight)
async def process_weight(message: Message, state: FSMContext):
    await state.update_data(weight=message.text)
    
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Отправить контакт", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await message.answer("Пожалуйста, поделитесь вашим номером телефона для связи:", reply_markup=kb)
    await state.set_state(OrderFSM.waiting_for_phone)

@router.message(OrderFSM.waiting_for_phone, F.contact | F.text)
async def process_phone(message: Message, state: FSMContext):
    phone = message.contact.phone_number if message.contact else message.text
    await state.update_data(phone=phone)
    
    from aiogram.types import ReplyKeyboardRemove
    await message.answer("Принято! Теперь прикрепите фото-референс или введите /skip:", 
                         reply_markup=ReplyKeyboardRemove())
    await state.set_state(OrderFSM.waiting_for_photo)

@router.message(OrderFSM.waiting_for_photo)
async def process_photo(message: Message, state: FSMContext):
    if message.photo:
        # Берем самое качественное фото (последнее в списке)
        photo_id = message.photo[-1].file_id
        await state.update_data(ref_photo=photo_id)
    elif message.text == "/skip":
        await state.update_data(ref_photo=None)
    else:
        await message.answer("Пожалуйста, пришлите фото или введите /skip")
        return

    await message.answer("Напишите комментарий к заказу (начинка, надпись и т.д.):")
    await state.set_state(OrderFSM.waiting_for_comment)

@router.message(OrderFSM.waiting_for_comment)
async def process_comment(message: Message, state: FSMContext):
    await state.update_data(comment=message.text)
    data = await state.get_data()
    
    product = data.get('product_name', 'Не указано')
    category = data.get('category', 'Не указано')
    full_item_name = f"{category}: {product}"
    phone = data.get('phone', 'Не указан')
    
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            "INSERT INTO orders (user_id, username, target_date, category, weight, ref_photo, comment) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (message.from_user.id, message.from_user.username, data.get('target_date'), 
             full_item_name, data.get('weight'), data.get('ref_photo'), message.text)
        )
        order_id = cursor.lastrowid
        await db.commit()

    # Вспомогательная функция для кнопок (лучше вынести её из обработчика)
    kb_builder = InlineKeyboardBuilder()
    kb_builder.button(text="✅ Подтвердить", callback_data=f"confirm_{order_id}_{message.from_user.id}")
    kb_builder.button(text="❌ Отклонить", callback_data=f"reject_{order_id}_{message.from_user.id}")
    admin_kb = kb_builder.as_markup()

    await message.answer(f"✅ Ваш заказ #{order_id} успешно оформлен! Ожидайте подтверждения.")
    
    admin_text = (f"🔔 <b>Новый заказ #{order_id}</b>\n"
                  f"👤 Клиент: @{message.from_user.username}\n"
                  f"📞 Тел: {phone}\n"
                  f"🎂 Изделие: <b>{full_item_name}</b>\n"
                  f"📅 Дата: {data.get('target_date')}\n"
                  f"⚖️ Вес: {data.get('weight')}\n"
                  f"📝 ТЗ: {message.text}")
    
    for admin in ADMIN_IDS:
        try:
            if data.get('ref_photo'):
                await bot.send_photo(admin, photo=data['ref_photo'], caption=admin_text, parse_mode="HTML", reply_markup=admin_kb)
            else:
                await bot.send_message(admin, admin_text, parse_mode="HTML", reply_markup=admin_kb)
        except Exception as e:
            print(f"Ошибка уведомления админа: {e}")

    await state.clear()

STATUS_MAP = {
    "pending": "⏳ В ожидании",
    "confirmed": "✅ Подтвержден",
    "cancelled": "❌ Отменен"
}

@router.callback_query(F.data == "admin_orders")
async def admin_view_orders(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("У вас нет доступа.", show_alert=True)
        return

    async with aiosqlite.connect(DB_NAME) as db:
        # Ищем все заказы, кроме отмененных
        async with db.execute(
            "SELECT id, username, target_date, category, weight, comment, ref_photo, status FROM orders WHERE status != 'cancelled'"
        ) as cursor:
            orders = await cursor.fetchall()

    if not orders:
        await callback.message.answer("Активных заказов пока нет.")
        await callback.answer()
        return

    for o_id, username, t_date, cat, weight, comment, photo, status in orders:
        # Красивый статус для кондитера
        status_emoji = "⏳ Ожидает" if status == "pending" else "✅ В работе"
        if status == "reminded":
            status_emoji = "⏰ Напомнено"

        text = (f"📦 <b>Заказ #{o_id}</b> [{status_emoji}]\n"
                f"👤 Клиент: @{username}\n"
                f"📅 Дата готовности: {t_date}\n"
                f"🎂 Изделие: {cat}\n"
                f"秤 Вес: {weight}\n"
                f"💬 ТЗ/Коммент: {comment}")

        if photo:
            try:
                await callback.message.answer_photo(photo, caption=text, parse_mode="HTML")
            except Exception:
                await callback.message.answer(text + "\n\n⚠️ (Не удалось загрузить фото-референс)", parse_mode="HTML")
        else:
            await callback.message.answer(text, parse_mode="HTML")
            
    await callback.answer() # Обязательно гасим часики на кнопке

@router.callback_query(F.data.startswith("confirm_"))
async def admin_confirm_order(callback: CallbackQuery):
    _, order_id, user_id = callback.data.split("_")
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE orders SET status = 'confirmed' WHERE id = ?", (order_id,))
        await db.commit()
    
    # Проверяем, есть ли подпись (фото) или просто текст
    new_status = "\n\n✅ <b>ПОДТВЕРЖДЕНО</b>"
    
    if callback.message.caption:
        await callback.message.edit_caption(
            caption=callback.message.caption + new_status, 
            parse_mode="HTML"
        )
    else:
        await callback.message.edit_text(
            text=callback.message.text + new_status, 
            parse_mode="HTML"
        )
        
    await bot.send_message(user_id, f"🌟 Ваш заказ №{order_id} подтвержден кондитером и принят в работу!")
    await callback.answer("Заказ подтвержден")

@router.callback_query(F.data.startswith("reject_"))
async def admin_reject_order(callback: CallbackQuery):
    _, order_id, user_id = callback.data.split("_")
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", (order_id,))
        await db.commit()
    
    new_status = "\n\n❌ <b>ОТКЛОНЕН</b>"
    
    if callback.message.caption:
        await callback.message.edit_caption(
            caption=callback.message.caption + new_status, 
            parse_mode="HTML"
        )
    else:
        await callback.message.edit_text(
            text=callback.message.text + new_status, 
            parse_mode="HTML"
        )
        
    await bot.send_message(user_id, f"😔 К сожалению, кондитер не сможет выполнить ваш заказ №{order_id} и отклонил его.")
    await callback.answer("Заказ отклонен")

@router.callback_query(F.data == "admin_broadcast")
async def start_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS: return
    await callback.message.answer("Введите текст рассылки. Можно прикрепить фото.")
    await state.set_state(BroadcastFSM.waiting_for_message)
    await callback.answer()

@router.message(BroadcastFSM.waiting_for_message)
async def do_broadcast(message: Message, state: FSMContext):
    # Получаем всех уникальных пользователей из базы заказов 
    # (или можно создать отдельную таблицу Users)
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            users = await cursor.fetchall()

    count = 0
    for (user_id,) in users:
        try:
            if message.photo:
                await bot.send_photo(user_id, photo=message.photo[-1].file_id, caption=message.caption or "")
            else:
                await message.copy_to(user_id) # Копирует текст, форматирование и т.д.
            count += 1
            await asyncio.sleep(0.05) # Защита от флуд-фильтра Telegram
        except Exception:
            pass

    await message.answer(f"✅ Рассылка завершена! Получили: {count} чел.")
    await state.clear()

@router.callback_query(F.data == "admin_price")
async def admin_price_main(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data="price_add")],
        [InlineKeyboardButton(text="🗑 Удалить товар", callback_data="price_view_delete")]
    ])
    await callback.message.edit_text("Управление прайс-листом:", reply_markup=kb)

# --- УДАЛЕНИЕ ---
@router.callback_query(F.data == "price_view_delete")
async def view_price_for_delete(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id, category, name FROM price_list ORDER BY category") as cursor:
            items = await cursor.fetchall()
    
    if not items:
        await callback.answer("Прайс пуст", show_alert=True)
        return

    buttons = []
    for i_id, cat, name in items:
        buttons.append([InlineKeyboardButton(text=f"❌ {cat}: {name}", callback_data=f"del_{i_id}")])
    
    await callback.message.edit_text("Выберите товар, который нужно удалить:", 
                                     reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data.startswith("del_"))
async def delete_price_item(callback: CallbackQuery):
    item_id = callback.data.split("_")[1]
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("DELETE FROM price_list WHERE id = ?", (item_id,))
        await db.commit()
    
    await callback.answer("Товар удален!")
    await admin_price_main(callback) # Возвращаемся в меню прайса

# --- ДОБАВЛЕНИЕ (обновленный вход) ---
@router.callback_query(F.data == "price_add")
async def admin_add_price_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите категорию (или выберите существующую):")
    # Можно также вывести кнопки с существующими категориями для удобства
    await state.set_state(AdminPriceFSM.waiting_for_category)

@router.message(AdminPriceFSM.waiting_for_category)
async def price_category(message: Message, state: FSMContext):
    category_name = message.text.strip().capitalize()
    await state.update_data(cat=category_name)
    await message.answer(f"Категория: {category_name}\nТеперь введите название изделия:")
    await state.set_state(AdminPriceFSM.waiting_for_name)

@router.message(AdminPriceFSM.waiting_for_name)
async def price_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("Введите цену (только цифры):")
    await state.set_state(AdminPriceFSM.waiting_for_price)

@router.message(AdminPriceFSM.waiting_for_price)
async def price_final(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Пожалуйста, введите только число.")
        return
    
    data = await state.get_data()
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO price_list (category, name, price) VALUES (?, ?, ?)",
            (data['cat'], data['name'], int(message.text))
        )
        await db.commit()
    
    await message.answer(f"✅ Товар '{data['name']}' добавлен в прайс!")
    await state.clear()


# --- 7. ОТМЕНА ЗАКАЗА ---
@router.callback_query(F.data == "cancel_order")
async def list_cancel_orders(callback: CallbackQuery):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id, target_date FROM orders WHERE user_id = ? AND status != 'cancelled'", (callback.from_user.id,)) as cursor:
            orders = await cursor.fetchall()
            
    if not orders:
        await callback.answer("У вас нет активных заказов.", show_alert=True)
        return

    kb_buttons = []
    for order_id, target_date_str in orders:
        target_date = datetime.strptime(target_date_str, "%Y-%m-%d")
        # Проверка лимитов на отмену
        if (target_date - datetime.now()).days >= MIN_DAYS_TO_CANCEL:
            kb_buttons.append([InlineKeyboardButton(text=f"Заказ #{order_id} ({target_date_str})", callback_data=f"do_cancel_{order_id}")])
    
    if not kb_buttons:
        await callback.message.answer("К сожалению, время для отмены ваших текущих заказов уже вышло.")
        return

    await callback.message.edit_text("Выберите заказ для отмены:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons))

@router.callback_query(F.data.startswith("do_cancel_"))
async def process_cancel(callback: CallbackQuery):
    order_id = callback.data.split("_")[2]
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", (order_id,))
        await db.commit()
    
    await callback.message.edit_text(f"✅ Заказ #{order_id} успешно отменен.")
    
    # Уведомление админам об отмене
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(admin, f"❌ Заказ #{order_id} был отменен клиентом.")
        except Exception:
            pass

async def reminder_scheduler():
    """Фоновая задача, которая проверяет заказы раз в час и присылает напоминания."""
    print("Запущен фоновый планировщик напоминаний...")
    while True:
        print("ℹ️ Планировщик проверяет базу данных...")
        try:
            # Высчитываем целевую дату на основе настроек из .env
            # Если торт нужен завтра, а напоминание за 24 часа — ищем заказы на завтра
            target_datetime = datetime.now() + timedelta(hours=REMINDER_HOURS_BEFORE)
            target_date_str = target_datetime.strftime("%Y-%m-%d")

            async with aiosqlite.connect(DB_NAME) as db:
                # Ищем подтвержденные заказы на эту дату, по которым еще не отправлено напоминание
                # Чтобы не спамить каждый час, мы временно проверяем статус 'confirmed'
                async with db.execute(
                    "SELECT id, username, category, weight, comment FROM orders WHERE target_date = ? AND status = 'confirmed'", 
                    (target_date_str,)
                ) as cursor:
                    orders = await cursor.fetchall()

                if orders:
                    for o_id, username, item_name, weight, comm in orders:
                        reminder_text = (
                            f"⏰ <b>НАПОМИНАНИЕ О ЗАКАЗЕ!</b>\n\n"
                            f"📦 <b>Заказ #{o_id}</b> должен быть готов через {REMINDER_HOURS_BEFORE} ч.\n"
                            f"👤 Клиент: @{username}\n"
                            f"🎂 Изделие: <b>{item_name}</b>\n"
                            f"⚖️ Вес: {weight}\n"
                            f"📝 Детали: {comm}"
                        )
                        
                        for admin in ADMIN_IDS:
                            try:
                                await bot.send_message(admin, reminder_text, parse_mode="HTML")
                            except Exception as e:
                                print(f"Не удалось отправить напоминание админу {admin}: {e}")
                        
                        # Обновляем статус заказа в 'reminded', чтобы бот не присылал уведомление повторно через час
                        await db.execute("UPDATE orders SET status = 'reminded' WHERE id = ?", (o_id,))
                    
                    await db.commit()

        except Exception as e:
            print(f"Ошибка в планировщике напоминаний: {e}")
        
        # Спим ровно 1 час (3600 секунд) перед следующей проверкой
        await asyncio.sleep(3600)


async def main():
    await init_db()
    dp.include_router(router)
    print("Bot is starting...")
    await bot.delete_webhook(drop_pending_updates=True)
    
    # Запускаем планировщик напоминаний в фоне асинхронно
    asyncio.create_task(reminder_scheduler())
    
    # Запускаем опрос бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())