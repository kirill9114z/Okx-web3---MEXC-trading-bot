import time
import json
import hashlib
import asyncio
from typing import Dict, Optional, Any
import aiohttp
# from config import current_config

# Потокобезопасный кэш с TTL (30 секунд)
_id_cache: Dict[str, tuple[str, float]] = {}
CACHE_TTL = 60

# Асинхронная блокировка для кэша
_cache_lock = asyncio.Lock()

# Сессия с connection pool и таймаутами
_session: Optional[aiohttp.ClientSession] = None
_session_lock = asyncio.Lock()


async def get_session() -> aiohttp.ClientSession:
    global _session
    async with _session_lock:
        if _session is None or _session.closed:
            connector = aiohttp.TCPConnector(
                limit=100,  # Максимум соединений
                ttl_dns_cache=3000  # Кэш DNS 5 минут
            )
            _session = aiohttp.ClientSession(
                connector=connector,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Origin": "https://www.mexc.com",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
                },
                timeout=aiohttp.ClientTimeout(
                    total=20,  # Общий таймаут
                    connect=10,  # Таймаут соединения
                    sock_read=10  # Таймаут чтения
                )
            )
    return _session


async def close_session():
    global _session
    async with _session_lock:
        if _session and not _session.closed:
            await _session.close()


async def get_current_ids(symbol: str, session, u_id):
    # Используем кэш с проверкой TTL
    async with _cache_lock:
        if symbol in _id_cache:
            cached_value, timestamp = _id_cache[symbol]
            if time.time() - timestamp < CACHE_TTL:
                return cached_value

    url = "https://www.mexc.com/api/assetbussiness/asset/spot/statistic/v2"
    headers = {
        "Referer": f"https://www.mexc.com/exchange/{symbol}",
        "Cookie": f"uc_token={u_id}; u_id={u_id};",
        "X-Requested-With": "XMLHttpRequest",
    }

    try:
        async with session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json()

            if data.get('code') == 401:
                print(f"Auth error: {data} for {symbol}")
                return False
            print(f'Data: {data}')
            if data.get('data'):
                for item in data['data']:
                    if item["currency"] == symbol.split('_')[0]:
                        vcoin_id = item["vcoinId"]
                        # Обновляем кэш
                        async with _cache_lock:
                            _id_cache[symbol] = (vcoin_id, time.time())
                        return vcoin_id
            else:
                return None
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        print(f"Error fetching vcoinId: {e}")
        return None


def generate_signature(u_id: str, data: dict) -> dict:
    """Синхронная генерация подписи с использованием пула процессов"""
    timestamp = str(int(time.time() * 1000))
    # Используем более эффективное хеширование
    h = hashlib.new('md5')
    h.update((u_id + timestamp).encode())
    g = h.hexdigest()[7:]

    # Оптимизированная сериализация
    s = json.dumps(data, separators=(',', ':'), ensure_ascii=False)

    h = hashlib.new('md5')
    h.update((timestamp + s + g).encode())
    return {'time': timestamp, 'sign': h.hexdigest()}


async def place_limit_order(
        symbol: str,
        price: float,
        amount: float,
        is_sell: bool,
        u_id,
        timeout: float = 3.0
):
    t = time.time()
    session = await get_session()
    vcoin_id = await get_current_ids(symbol, session, u_id)
    if vcoin_id == False:
        return False
    if vcoin_id is None:
        print(f'1')
        return None

    trade_type = 1 if is_sell else 0
    side_str = "SELL" if is_sell else "BUY"

    # Формируем данные для подписи (точно как в рабочем синхронном коде)
    sign_data = {
        "symbol": symbol,
        "side": side_str,  # Важно: строка "SELL"/"BUY", а не число!
        "openType": 1,
        "type": "1",
        "vol": amount,
        "price": f"{price}",
        "priceProtect": "0"
    }
    signature = generate_signature(u_id, sign_data)

    # Формируем тело запроса (точно как в синхронной версии)
    body = {
        "currencyId": vcoin_id,
        "marketCurrencyId": "128f589271cb4951b03e71e6323eb7be",
        "tradeType": trade_type,  # Число 1/0
        "orderType": "LIMIT_ORDER",
        "price": price,
        "quantity": str(amount),
    }

    headers = {
        "Referer": f"https://www.mexc.com/exchange/{symbol}",
        "Cookie": f"uc_token={u_id}; u_id={u_id}",
        "x-mxc-sign": signature["sign"],
        "x-mxc-nonce": signature["time"],
        "X-Requested-With": "XMLHttpRequest",
    }

    url = "https://www.mexc.com/api/platform/spot/order/place"

    try:
        async with session.post(
                url,
                headers=headers,
                json=body,
                timeout=timeout
        ) as resp:
            resp.raise_for_status()
            print(f"ВРЕМЯ ВЫПОЛНЕНИЯ: {time.time() - t}")
            return await resp.json()

    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        return {"error": str(e)}


