import logging
import time

import aiohttp
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from database import Database
import asyncio
from exchange import get_session
from main import main as arb_main
from main import active_arbitrage_instances

from config import BOT_TOKEN

db = Database()
arb_task: asyncio.Task | None = None
# Состояния FSM
class Form(StatesGroup):
    SETTINGS = State()
    GLOBAL_SPREAD_INPUT = State()
    INDIVIDUAL_SPREAD_SELECT = State()
    INDIVIDUAL_SPREAD_INPUT = State()
    PAIRS_MENU = State()
    ADD_PAIR_NAME = State()
    ADD_PAIR_CONTRACT = State()
    REMOVE_PAIR_SELECT = State()
    UID_INPUT = State()
    PRIVATE_KEY_INPUT = State()


bot_process = None
# Инициализация бота
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
chat_id = 0
# =====================
# ОСНОВНЫЕ ОБРАБОТЧИКИ
# =====================

@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    global chat_id, arb_task, bot
    builder = ReplyKeyboardBuilder()
    builder.button(text="⚙️ Настройки")
    builder.button(text="🔄 Пары")
    builder.button(text="🆔 Обновить U_ID")
    builder.button(text="🚀 Запустить бота")
    builder.button(text="🛑 Остановить бота")
    builder.adjust(2, 2, 1)
    chat_id = message.chat.id
    if not db.get_uid():
        await message.answer("Укажите ваш U_ID:")
        await state.set_state(Form.UID_INPUT)
    else:
        await message.answer(
            "Добро пожаловать в Arbitrage Bot!\nВыберите действие:",
            reply_markup=builder.as_markup(resize_keyboard=True)
        )
        await state.set_state(Form.SETTINGS)


@dp.message(Form.UID_INPUT)
async def process_uid(message: types.Message, state: FSMContext):
    """Обработчик ввода U_ID"""
    uid_value = message.text.strip()
    print(f"New id: {uid_value}")
    db.set_uid(uid_value)
    await message.answer(f"✅ U_ID успешно сохранен: {uid_value}")
    await message.answer("🔐 Введите свой private_key:")
    await state.set_state(Form.PRIVATE_KEY_INPUT)
    # await cmd_start(message, state)
@dp.message(Form.PRIVATE_KEY_INPUT)
async def process_private_key(message: types.Message, state: FSMContext):
    raw_key = message.text.strip()
    db.set_private_key(raw_key)
    await message.answer("✅ private_key сохранён безопасно!")
    return await cmd_start(message, state)

@dp.message(Form.SETTINGS, F.text == "🚀 Запустить бота")
async def start_arbitrage_bot(message: types.Message):
    global arb_task, bot
    pairs = db.get_all_pairs()
    if not pairs:
        return await message.answer(
            "❌ Нельзя запустить бота без торговых пар!\n"
            "Добавьте пары через меню \"🔄 Пары\""
        )
    if arb_task and not arb_task.done():
        return await message.answer("✅ Бот уже запущен")
    # запускаем задачу арбитража в фоне
    arb_task = asyncio.create_task(arb_main(chat_id, bot))
    # for pair in pairs:
    #     arb = active_arbitrage_instances[pair]
    #     arb.running = True
    await message.answer("🚀 Арбитражный бот успешно запущен!")


@dp.message(Form.SETTINGS, F.text == "🛑 Остановить бота")
async def stop_arbitrage_bot(message: types.Message):
    global arb_task
    # if not arb_task:
    #     return await message.answer("ℹ️ Бот не запущен")
    # if arb_task.done():
    #     arb_task = None
    #     return await message.answer("ℹ️ Бот уже остановлен")
    #
    # arb_task.cancel()
    # try:
    #     await arb_task
    # except asyncio.CancelledError:
    #     # Это нормально — задача отменена
    #     pass
    # arb_task = None
    print(f'Lst: {active_arbitrage_instances}')
    pairs = db.get_all_pairs()
    for pair in pairs:
        try:
            arb = active_arbitrage_instances[pair]
            arb.running = False
        except KeyError:
            continue
    arb_task.cancel()
    arb_task = None
    await message.answer("🛑 Арбитражный бот остановлен!")


@dp.message(Form.SETTINGS, F.text == "🆔 Обновить U_ID")
async def update_uid_handler(message: types.Message, state: FSMContext):
    # Сохраняем текущее состояние, чтобы вернуться к нему позже
    await state.update_data(prev_state=await state.get_state())

    # Запрашиваем новый U_ID
    await message.answer(
        "Пожалуйста, введите ваш новый U_ID:",
        reply_markup=types.ReplyKeyboardRemove()
    )

    # Устанавливаем состояние ожидания U_ID
    await state.set_state(Form.UID_INPUT)

    # Сохраняем информацию о том, что это обновление существующего U_ID
    await state.update_data(is_update=True)


@dp.message(Form.UID_INPUT)
async def process_uid(message: types.Message, state: FSMContext):
    """Обработчик ввода U_ID"""
    data = await state.get_data()
    uid_value = message.text.strip()

    # Проверяем, что это обновление существующего U_ID
    if data.get('is_update'):
        # Сохраняем новый U_ID
        db.set_uid(uid_value)
        await message.answer(f"✅ U_ID успешно обновлен: {uid_value}")

        # Возвращаемся в предыдущее состояние
        prev_state = data.get('prev_state', Form.SETTINGS)
        await state.set_state(prev_state)

        # Возвращаемся в главное меню
        await cmd_start(message, state)
    else:
        # Обработка первоначального ввода U_ID
        print(f"New id: {uid_value}")
        db.set_uid(uid_value)
        await message.answer(f"✅ U_ID успешно сохранен: {uid_value}")
        await cmd_start(message, state)
# =====================
# НАСТРОЙКИ
# =====================

@dp.message(Form.SETTINGS, F.text == "⚙️ Настройки")
async def settings_menu(message: types.Message, state: FSMContext):
    builder = ReplyKeyboardBuilder()
    builder.button(text="🌐 Глобальный спред")
    builder.button(text="🎯 Индивидуальный спред")
    builder.button(text="🔙 Назад")
    builder.adjust(2)

    await message.answer(
        "Настройки спреда:",
        reply_markup=builder.as_markup(resize_keyboard=True)
    )


# Глобальный спред
@dp.message(Form.SETTINGS, F.text == "🌐 Глобальный спред")
async def global_spread_start(message: types.Message, state: FSMContext):
    current = db.get_global_spread()
    await message.answer(
        f"Текущий глобальный спред: {current}%\n"
        "Введите новое значение (0 < spread < 100):",
        reply_markup=types.ReplyKeyboardRemove()
    )
    await state.set_state(Form.GLOBAL_SPREAD_INPUT)


@dp.message(Form.GLOBAL_SPREAD_INPUT)
async def global_spread_set(message: types.Message, state: FSMContext):
    try:
        spread = float(message.text)
        if 0 < spread < 100:
            db.set_global_spread(spread)
            await message.answer(f"✅ Глобальный спред установлен: {spread}%")
            return await cmd_start(message, state)
        raise ValueError()
    except:
        await message.answer("❌ Ошибка! Введите число между 0 и 100:")


# Индивидуальный спред
@dp.message(Form.SETTINGS, F.text == "🎯 Индивидуальный спред")
async def individual_spread_start(message: types.Message, state: FSMContext):
    pairs = db.get_all_pairs()
    if not pairs:
        await message.answer("ℹ️ Сначала добавьте пары через меню 'Пары'")
        return await settings_menu(message, state)

    builder = ReplyKeyboardBuilder()
    for pair in pairs:
        builder.button(text=pair)
    builder.button(text="🔙 Назад")
    builder.adjust(2)

    await message.answer(
        "Выберите пару для настройки:",
        reply_markup=builder.as_markup(resize_keyboard=True)
    )
    await state.set_state(Form.INDIVIDUAL_SPREAD_SELECT)


@dp.message(Form.INDIVIDUAL_SPREAD_SELECT)
async def individual_spread_select(message: types.Message, state: FSMContext):
    pair = message.text.upper()
    pairs = db.get_all_pairs()

    if pair == "🔙 НАЗАД":
        return await settings_menu(message, state)

    if pair not in pairs:
        await message.answer("❌ Пара не найдена! Выберите из списка:")
        return

    await state.update_data(selected_pair=pair)
    current = db.get_pair_spread(pair) or "не установлен"
    await message.answer(
        f"Текущий спред для {pair}: {current}%\n"
        "Введите новое значение (0 < spread < 100):",
        reply_markup=types.ReplyKeyboardRemove()
    )
    await state.set_state(Form.INDIVIDUAL_SPREAD_INPUT)


@dp.message(Form.INDIVIDUAL_SPREAD_INPUT)
async def individual_spread_set(message: types.Message, state: FSMContext):
    try:
        spread = float(message.text)
        if not (0 < spread < 100):
            raise ValueError()

        data = await state.get_data()
        pair = data['selected_pair']
        db.set_pair_spread(pair, spread)
        await message.answer(f"✅ Спред для {pair} установлен: {spread}%")
        return await cmd_start(message, state)
    except:
        await message.answer("❌ Ошибка! Введите число между 0 и 100:")


# =====================
# УПРАВЛЕНИЕ ПАРАМИ
# =====================

@dp.message(Form.SETTINGS, F.text == "🔄 Пары")
async def pairs_menu(message: types.Message, state: FSMContext):
    builder = ReplyKeyboardBuilder()
    builder.button(text="➕ Добавить пару")
    builder.button(text="➖ Удалить пару")
    builder.button(text="🔙 Назад")
    builder.adjust(2)

    await message.answer(
        "Управление торговыми парами:",
        reply_markup=builder.as_markup(resize_keyboard=True))
    await state.set_state(Form.PAIRS_MENU)


# Добавление пары
@dp.message(Form.PAIRS_MENU, F.text == "➕ Добавить пару")
async def add_pair_name(message: types.Message, state: FSMContext):
    await message.answer(
        "Введите название пары (например: BTC/USDT):",
        reply_markup=types.ReplyKeyboardRemove()
    )
    await state.set_state(Form.ADD_PAIR_NAME)


@dp.message(Form.ADD_PAIR_NAME)
async def process_pair_name(message: types.Message, state: FSMContext):
    pair = message.text.upper()
    # if not pair.isalnum() or len(pair) < 5:
    #     await message.answer("❌ Некорректный формат! Пример: BTCUSDT")
    #     return

    # Проверяем, существует ли уже такая пара
    if db.get_pair_contracts(pair):
        await message.answer(f"❌ Пара {pair} уже существует!")
        return await cmd_start(message, state)

    await state.update_data(new_pair=pair)

    # Новое сообщение с инструкцией
    await message.answer(
        "Введите контракты через запятую в следующем порядке:\n\n"
        "1. Ethereum контракт\n"
        "2. Base контракт\n"
        "3. BSC контракт\n\n"
        "Пример: 0xaaa...,0xbbb...,0xccc...",
        reply_markup=types.ReplyKeyboardRemove()
    )
    await state.set_state(Form.ADD_PAIR_CONTRACT)


@dp.message(Form.ADD_PAIR_CONTRACT)
async def process_pair_contract(message: types.Message, state: FSMContext):
    contracts_input = message.text.strip()
    contracts = [c.strip() for c in contracts_input.split(',')]

    # Проверяем количество контрактов
    if len(contracts) != 3:
        await message.answer(
            "❌ Ошибка! Должно быть ровно 3 контракта, разделенных запятыми.\n"
            "Пожалуйста, введите снова:"
        )
        return

    # Проверяем валидность контрактов (базовая проверка длины)
    # if any(len(c) < 20 for c in contracts):
    #     await message.answer(
    #         "❌ Ошибка! Один или несколько контрактов выглядят невалидными.\n"
    #         "Пожалуйста, проверьте и введите снова:"
    #     )
    #     return

    data = await state.get_data()
    pair = data['new_pair']

    # Сохраняем как кортеж (ethereum, base, bsc)
    if db.add_pair(pair, (contracts[0], contracts[1], contracts[2])):
        contracts_info = (
            f"\n• Ethereum: `{contracts[0]}`"
            f"\n• Base: `{contracts[1]}`"
            f"\n• BSC: `{contracts[2]}`"
        )
        await message.answer(
            f"✅ Пара *{pair}* добавлена! Контракты:" + contracts_info,
            parse_mode="Markdown"
        )
    else:
        await message.answer(f"❌ Ошибка при добавлении пары {pair}!")

    await cmd_start(message, state)


# Удаление пары
# Инициация меню удаления
@dp.message(Form.PAIRS_MENU, F.text == "➖ Удалить пару")
async def remove_pair_start(message: types.Message, state: FSMContext):
    pairs = db.get_all_pairs()
    if not pairs:
        await message.answer("ℹ️ Нет пар для удаления")
        return await pairs_menu(message, state)

    builder = ReplyKeyboardBuilder()
    for pair in pairs:
        builder.button(text=pair)
    builder.button(text="🔙 Назад")
    builder.adjust(2)

    await message.answer(
        "Выберите пару для удаления:",
        reply_markup=builder.as_markup(resize_keyboard=True)
    )
    await state.set_state(Form.REMOVE_PAIR_SELECT)


@dp.message(Form.REMOVE_PAIR_SELECT, F.text == "🔙 Назад")
async def remove_pair_back(message: types.Message, state: FSMContext):
    await state.clear()
    await cmd_start(message, state)


@dp.message(Form.REMOVE_PAIR_SELECT)
async def process_remove_pair(message: types.Message, state: FSMContext):
    pair = message.text.upper()
    if db.remove_pair(pair):
        await message.answer(f"✅ Пара {pair} удалена!")
    else:
        await message.answer(f"❌ Пара {pair} не найдена или не удалена.")
    await state.clear()
    await cmd_start(message, state)


# @dp.message(Form.__all_states__, F.text == "🔙 Назад")
# async def any_back(message: types.Message, state: FSMContext):
#     await state.clear()
#     await cmd_start(message, state)

@dp.message(Form.REMOVE_PAIR_SELECT, F.text == "🔙 Назад")
async def back_from_remove_pair(message: types.Message, state: FSMContext):
    # из меню удаления → возвращаемся в меню «Пары»
    await pairs_menu(message, state)

@dp.message(Form.PAIRS_MENU, F.text == "🔙 Назад")
async def back_from_pairs(message: types.Message, state: FSMContext):
    # из меню «Пары» → в «Настройки»
    await settings_menu(message, state)

@dp.message(Form.SETTINGS, F.text == "🔙 Назад")
async def back_from_settings(message: types.Message, state: FSMContext):
    # из «Настройки» → в главное меню
    await cmd_start(message, state)


# =====================
# ЗАПУСК БОТА
# =====================

@dp.callback_query(F.data.startswith("execute_"))
async def execute_arbitrage(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.answer("Запрос принят, проверяем...")
        logging.info(f'Начали анализ')
        data = callback.data.split('_')
        # if len(data) != 6:
        #     await callback.answer("Ошибка в данных сделки")
        #     return
        print(f'Data tg: {data}, {len(data)}')
        # Парсим данные из callback
        _, opp_type, mexc, pair, chain_id, value, mexc_price, decimal = data
        chain_id = int(chain_id)
        id3 = {1: 'ethereum', 8453: 'base', 56: 'bsc'}
        contract = db.get_pair_contracts(pair)[id3[chain_id]]
        # Проверяем актуальность сделки (30 секунд)
        # if time.time() - timestamp > 30:
        #     await callback.answer("Сделка устарела", show_alert=True)
        #     await callback.message.edit_reply_markup(reply_markup=None)
        #     return
        await callback.answer("Проверяем актуальность сделки...")

        # Получаем экземпляр Arbitrage из main.py
        arbitrage = active_arbitrage_instances.get(pair)
        if not arbitrage:
            await callback.message.answer(f"❌ Мониторинг для пары {pair} не активен")
            return

        # Проверяем актуальность сделки
        opportunity = {
            'type': f'{opp_type}_{mexc}',
            'volume': f"{value}",
            'chain_id': chain_id,
            'contract': contract,
            'mexc_price': float(mexc_price),
            'decimals': decimal
        }
        t = time.time()
        print(f'Обработка check now profit')
        is_profitable, profit, message = await arbitrage.check_now_profit(opportunity)
        print(f'Закончили: {time.time() - t}')
        if is_profitable:
            return
        else:
            await callback.message.answer(f"❌ Сделка больше не актуальна: {message}")
            return
    except Exception as e:
        await callback.message.answer(f'❌ Ошибка в сделке: {e}')
async def main1():
    await dp.start_polling(bot)


async def request_new_uid(chat_id: int):
    await bot.send_message(
        chat_id,
        "⚠️ Ваш U_ID устарел. Пожалуйста, введите новый U_ID:"
    )


if __name__ == "__main__":
    import asyncio
    asyncio.run(main1())

