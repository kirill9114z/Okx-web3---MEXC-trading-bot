import asyncio
import random
import time
from datetime import datetime, timezone
import hmac
import hashlib
import base64
import requests
import json
from config import OKX_SECRET_KEY, OKX_API_KEY, OKX_PASSPHRASE, RPC_BASE, RPC_BSC, RPC_ETH
from exchange import get_session
price_bnb = 700
price_eth = 3500
# Конфигурация (ваши ключи)

WALLET_ADDRESS = "0xE1103b3FfC9820B65b493f64d02F85F27360537e"

# Обновленная конфигурация сетей с добавлением BSC
NETWORK_CONFIG = {
    '8453': {
        'id': '8453',
        'rpc': RPC_BASE,
        'coin_id': 'ethereum',
        'transfer_gas': 65000,
        'usdc_address': '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913',
        # 'aave_address': '0x63706e401c06ac8513145b7687a14804d17f814b',
        'usdc_decimals': 6
    },
    '1': {
        'id': '1',
        'rpc': RPC_ETH,
        'coin_id': 'ethereum',
        'transfer_gas': 65000,
        'usdc_address': '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48',
        # 'aave_address': '0x7Fc66500c84A76Ad7e9c93437bFc5Ac33E2DDaE9',
        'usdc_decimals': 6
    },
    '56': {  # Добавлена конфигурация для Binance Smart Chain
        'id': '56',
        'rpc': RPC_BSC,
        'coin_id': 'binancecoin',
        'transfer_gas': 50000,  # Обычно ниже, чем в Ethereum
        'usdc_address': '0x8965349fb649A33a30cbFDa057D8eC2C48AbE2A2',  # BSC USDC
        # 'aave_address': '0xfb6115445Bff7b52FeB98650C87f44907E58f802',  # BSC AAVE
        'usdc_decimals': 18  # Важно: на BSC USDC имеет 18 decimals
    }
}


# def generate_okx_headers(method: str, path: str, body: str = "") -> dict:
#     """Генерация заголовков для OKX API"""
#     timestamp = str(datetime.now(timezone.utc).isoformat(timespec="milliseconds")).replace("+00:00", "Z")
#     message = timestamp + method + path + body
#     signature = base64.b64encode(
#         hmac.new(OKX_SECRET_KEY.encode(), message.encode(), hashlib.sha256).digest()
#     ).decode()
#     return {
#         "OK-ACCESS-KEY": OKX_API_KEY,
#         "OK-ACCESS-SIGN": signature,
#         "OK-ACCESS-TIMESTAMP": timestamp,
#         "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
#         "Content-Type": "application/json"
#     }
def generate_okx_headers(method: str, request_path: str, body: str = "") -> dict:
    """
    method: "GET" или "POST"
    request_path: путь запроса, напр. "/api/v5/dex/market/price" (для GET если есть query — добавьте ?a=1&b=2)
    body: строка тела запроса (для POST) — должна быть точно такой же, какая отправляется.
    """
    method = method.upper()
    timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    message = timestamp + method + request_path + (body or "")
    signature = base64.b64encode(
        hmac.new(OKX_SECRET_KEY.encode(), message.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json"
    }

async def get_swap_quote(chain, amount, address, session):
    """Универсальная функция для получения котировок"""
    config = NETWORK_CONFIG.get(chain)
    if not config:
        raise ValueError(f"Unsupported chain: {chain}")

    url = "https://www.okx.com/api/v5/dex/aggregator/quote"
    amount_in_units = int(amount * (10 ** config['usdc_decimals']))
    params = {
        "chainId": config['id'],
        "amount": str(amount_in_units),
        "fromTokenAddress": config['usdc_address'],
        "toTokenAddress": config['aave_address'],
        "slippage": "0.5",
        "userWalletAddress": WALLET_ADDRESS
    }

    # Сортировка параметров для корректной подписи
    # sorted_params = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    # path_for_sign = f"/api/v5/dex/aggregator/quote?{sorted_params}"
    headers = generate_okx_headers("GET",
                                   "/api/v5/dex/aggregator/quote?" + "&".join(f"{k}={v}" for k, v in params.items()))

    try:
        async with session.get(url, params=params, headers=headers) as response:
            data = await response.json()
            code = data.get("code")
        if code == "50011":  # Too Many Requests
            await asyncio.sleep(random.randint(10, 20))
            return None
        if code == "82112":  # liquidity
            # print(f'Недостаточно ликвы для: {address}')
            return None
        if code == "51000":  # bad parameter
            return None
        return data["data"][0]
    except asyncio.TimeoutError as e:
        print(f"HTTP error getting quote for {chain}: {e}")
        return None
    except Exception as e:
        print(f"Unexpected error in get_swap_quote for {chain}: {e}")
        raise

_GAS_CACHE = {}


async def get_gas_price(chain, session):
    """Получение цены газа с обработкой ошибок"""
    config = NETWORK_CONFIG.get(chain)
    if not config:
        raise ValueError(f"Unsupported chain: {chain}")
    entry = _GAS_CACHE.setdefault(chain, {
        'price': None,
        'timestamp': 0,
        'lock': asyncio.Lock()
    })
    now = time.time()
    # если кеш свежее 60 секунд — отдаем сразу
    if entry['price'] is not None and now - entry['timestamp'] <= 60:
        return entry['price']

    # иначе — блокируемся, чтобы только один запрос шел к RPC
    # async with entry['lock']:
    #     # повторная проверка после ожидания лока
    #     now = time.time()
    #     if entry['price'] is not None and now - entry['timestamp'] < 60:
    #         return entry['price']
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_gasPrice",
        "params": [],
        "id": 1
    }

    try:
        async with session.post(config['rpc'], json=payload) as response:
        # response = requests.post(config['rpc'], json=payload, timeout=10)
            response.raise_for_status()
            result = await response.json()

            gas_price = int(result.get('result', '0x0'), 0)
            entry['price'] = gas_price
            entry['timestamp'] = int(time.time())
            return gas_price
    except Exception as e:
        print(f"Error getting gas price for {chain}: {str(e)}")
        # Возвращаем безопасное значение по умолчанию
        default_gas = {
            'ethereum': 30 * 10 ** 9,
            'base': 0.1 * 10 ** 9,
            'bsc': 5 * 10 ** 9
        }
        return default_gas.get(chain, 10 * 10 ** 9)


async def get_native_token_price(coin_id, session):
    """Получение цены с обработкой ошибок"""
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd"
    await asyncio.sleep(random.randint(5, 10))
    try:
        async with session.get(url) as response:
        # response = requests.get(url, timeout=10)
            response.raise_for_status()
            data = await response.json()
            price = data[coin_id]['usd']
            return price
    except Exception as e:
        print(f"Error getting price for {coin_id}: {str(e)}")
        # Возвращаем среднее значение при ошибке
        default_prices = {
            'ethereum': 3500,
            'binancecoin': 600,
        }
        return default_prices.get(coin_id, 1)


f = 1
QUOTE_CACHE = {}


async def calculate_total_gas_cost(chain, address, session, amount=0.2):
    global price_bnb, price_eth, f, QUOTE_CACHE
    NETWORK_CONFIG[chain]['aave_address'] = address
    config = NETWORK_CONFIG.get(chain)
    if not config:
        raise ValueError(f"Unsupported chain: {chain}")

    if f:
        price_eth = await get_native_token_price('ethereum', session)
        await asyncio.sleep(10)
        price_bnb = await get_native_token_price('binancecoin', session)
        f = 0

    try:
        cache_key = (chain, address.lower())
        now = time.time()
        cache_entry = QUOTE_CACHE.get(cache_key)

        if cache_entry and now - cache_entry['timestamp'] < random.randint(120, 200):
            trade_fee_usd = cache_entry['trade_fee']
        else:
            quote = await get_swap_quote(chain, amount, address, session)
            if quote == None:
                return 100, 100
            trade_fee_usd = float(quote['tradeFee'])
            QUOTE_CACHE[cache_key] = {
                'timestamp': now,
                'trade_fee': trade_fee_usd
            }
            if len(QUOTE_CACHE) > 100:
                oldest = min(QUOTE_CACHE.items(), key=lambda x: x[1]['timestamp'])[0]
                del QUOTE_CACHE[oldest]

        gas_price_wei = await get_gas_price(chain, session)
        transfer_cost_wei = gas_price_wei * config['transfer_gas']
        transfer_cost_native = transfer_cost_wei / 10 ** 18
        transfer_cost_usd = float(transfer_cost_native) * float(price_bnb if chain == 'bsc' else price_eth)

        return trade_fee_usd, transfer_cost_usd

    except Exception as e:
        print(f"Ошибка при расчёте газа для {chain}: {e}")
        return 100, 100


async def price_calc(chain, amount, address, session, buy, decimals=None):
    try:
        config = NETWORK_CONFIG.get(chain)
        if not config:
            raise ValueError(f"Unsupported chain: {chain}")

        url = "https://www.okx.com/api/v5/dex/aggregator/quote"
        transfer_cost_usd = 0
        if buy:
            amount_in_units = int(amount * (10 ** config['usdc_decimals']))
            params = {
                "chainId": config['id'],
                "amount": str(amount_in_units),
                "fromTokenAddress": config['usdc_address'],
                "toTokenAddress": address,
                "slippage": "0.5",
                "userWalletAddress": WALLET_ADDRESS
            }
            gas_price_wei = await get_gas_price(chain, session)
            transfer_cost_wei = gas_price_wei * config['transfer_gas']
            transfer_cost_native = transfer_cost_wei / 10 ** 18
            transfer_cost_usd += float(transfer_cost_native) * float(price_bnb if chain == 'bsc' else price_eth)
            headers = generate_okx_headers("GET",
                                           "/api/v5/dex/aggregator/quote?" + "&".join(
                                               f"{k}={v}" for k, v in params.items()))

            async with session.get(url, params=params, headers=headers) as response:
                data = await response.json()

            if data.get("code") != "0":
                error_msg = data.get('msg', 'Unknown error')
                raise Exception(f"API error: {error_msg}, code: {data.get('code')}")
            transfer_cost_usd += float(data['data'][0]['tradeFee'])
            token_price = (float(data['data'][0]["fromTokenAmount"])/(10 ** config['usdc_decimals'])) / (float(data['data'][0]['toTokenAmount'])/10 ** (int(data['data'][0]['toToken']['decimal'])))
            return token_price, transfer_cost_usd

        else:
            amount_in_units = int(amount * (10 ** int(decimals)))
            params = {
                "chainId": config['id'],
                "amount": str(amount_in_units),
                "fromTokenAddress": address,
                "toTokenAddress": config['usdc_address'],
                "slippage": "0.5",
                "userWalletAddress": WALLET_ADDRESS
            }
            headers = generate_okx_headers("GET",
                                           "/api/v5/dex/aggregator/quote?" + "&".join(
                                               f"{k}={v}" for k, v in params.items()))

            async with session.get(url, params=params, headers=headers) as response:
                data = await response.json()

            if data.get("code") != "0":
                error_msg = data.get('msg', 'Unknown error')
                raise Exception(f"API error: {error_msg}, code: {data.get('code')}")
            transfer_cost_usd += float(data['data'][0]['tradeFee'])
            token_price = (float(data['data'][0]["toTokenAmount"])/(10 ** config['usdc_decimals'])) / (float(data['data'][0]['fromTokenAmount'])/10 ** (int(data['data'][0]['fromToken']['decimal'])))
            return token_price, transfer_cost_usd
    except Exception as e:
        if str(e) == 'OKX API error: Too Many Requests, code: 50011':
            time.sleep(random.randint(10, 20))
            return None, None
        if (
                str(e) == 'API error: The value difference from this transaction’s quote route is higher than 90%, which may lead to a risk of loss to user assets.') or (
                str(data.get('code')) == '82112'):
            print(f'Недостаточно ликвы для1: {address}')
            return None, None
        if (str(e) == 'API error: Parameter amount error') or (str(data.get('code')) == '51000'):
            return None, None
        else:
            print(f"Error in get_swap_quote for {chain}: \n{e}\n")
        raise


async def price_okx(chain_index: str | int, token_addr: str, session):
    path = "/api/v5/dex/market/price"
    url = "https://web3.okx.com" + path

    # Тело вызова — массив объектов, как в вашей попытке
    body_obj = [{"chainIndex": str(chain_index), "tokenContractAddress": token_addr.lower()}]

    # Очень важно: строка body, которая будет использована в подписи, должна быть
    # точной строкой, которую вы отправите в запросе.
    # Убираем пробелы для детерминированности.
    body_json = json.dumps(body_obj, separators=(",", ":"), ensure_ascii=False)

    # Подпись должна использовать request_path (без домена) и тело.
    headers = generate_okx_headers("POST", path, body_json)

    # Для aiohttp: используем data=body_json, чтобы тело не сериализовалось заново и совпадало с подписью
    async with session.post(url, data=body_json.encode("utf-8"), headers=headers) as resp:
        raw = await resp.text()
        try:
            data = json.loads(raw)
        except Exception:
            raise RuntimeError(f"OKX returned non-json response: {raw}")
        print(f'1 {raw}\n{data}')
        # По спецификации успех = code == "0"
        if str(data.get("code")) != "0":
            raise RuntimeError(f"OKX error: code={data.get('code')}, msg={data.get('msg')}, data={data}")

        rows = data.get("data") or []
        if not rows:
            raise RuntimeError(f"OKX returned empty data for chainIndex={chain_index}, token={token_addr}")

        return rows[0]["price"]

async def get_spender_address(BASE_CHAIN_ID, USDC_ADDRESS):
    """Получение адреса spender (dexContractAddress) от OKX"""
    try:
        url = "https://www.okx.com/api/v5/dex/aggregator/approve-transaction"
        params = {
            "chainId": BASE_CHAIN_ID,
            "tokenContractAddress": USDC_ADDRESS,
            "approveAmount": "1000000"  # Любое значение, важно получить spender
        }
        headers = generate_okx_headers("GET", "/api/v5/dex/aggregator/approve-transaction?" + "&".join(
            f"{k}={v}" for k, v in params.items()))

        response = requests.get(url, params=params, headers=headers)
        data = response.json()

        if data.get("code") != "0":
            raise Exception(f"Spender error: {data.get('msg')}")

        return data["data"][0]["dexContractAddress"]
    except Exception as e:
        if str(e) == 'Spender error: Too Many Requests':
            await asyncio.sleep(random.randint(5, 12))
            res = await get_spender_address(BASE_CHAIN_ID, USDC_ADDRESS)
            return res
        else:
            print(f'Что за спендер вообще такой: {e}')


# async def generate_okx_headers(method: str, path: str, body: str = "") -> dict:
#     """Генерация заголовков для OKX API :cite[7]"""
#     timestamp = str(datetime.now(timezone.utc).isoformat(timespec="milliseconds")).replace("+00:00", "Z")
#     message = timestamp + method + path + body
#     signature = base64.b64encode(
#         hmac.new(OKX_SECRET_KEY.encode(), message.encode(), hashlib.sha256).digest()
#     ).decode()
#     return {
#         "OK-ACCESS-KEY": OKX_API_KEY,
#         "OK-ACCESS-SIGN": signature,
#         "OK-ACCESS-TIMESTAMP": timestamp,
#         "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
#         "Content-Type": "application/json"
#     }
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    },
    {
            "constant": True,
            "inputs": [
                {"name": "owner", "type": "address"},
                {"name": "spender", "type": "address"}
            ],
            "name": "allowance",
            "outputs": [{"name": "", "type": "uint256"}],
            "type": "function",
            "stateMutability": "view"
        },
]
