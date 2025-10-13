import asyncio
import json
import hmac
import hashlib
import base64
import logging
import random
import time
import requests
from datetime import datetime, timezone
import websockets
import aiohttp
from typing import Optional
from web3 import Web3
from eth_account import Account
from web3.middleware import ExtraDataToPOAMiddleware

from okx_support_getprice import get_token_price
from config import OKX_SECRET_KEY, OKX_API_KEY, OKX_PASSPHRASE

# Сессия с connection pool и таймаутами
_session: Optional[aiohttp.ClientSession] = None
_session_lock = asyncio.Lock()

ERC20_ABI_DECIMALS = json.loads("""[
  {"constant":true,"inputs":[],"name":"decimals","outputs":[{"name":"","type":"uint8"}],"type":"function"}
]""")
ERC20_ABI_TRANSFER = json.loads("""[
  {"constant":false,"inputs":[{"name":"_to","type":"address"},{"name":"_value","type":"uint256"}],
   "name":"transfer","outputs":[{"name":"","type":"bool"}],"type":"function"}
]""")

ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
        "stateMutability": "view"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
        "stateMutability": "view"
    },
    {
        "constant": False,
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"}
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
        "stateMutability": "nonpayable"
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
    {
        "constant": False,
        "inputs": [
            {"name": "recipient", "type": "address"},
            {"name": "amount", "type": "uint256"}
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
        "stateMutability": "nonpayable"
    }
]

class OkxTrade:
    def __init__(self, pair, bd, exchange, w3_providers, privat_key):
        self.pair = pair
        self.bd = bd
        self.exchange = exchange

        self.w3 = w3_providers
        # for chain in (56, 8453):  # BSC, Base
        #     self.w3[chain].middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        self.decimals = {}

        self.api_key = OKX_API_KEY
        self.secret_key = OKX_SECRET_KEY
        self.passphrase = OKX_PASSPHRASE
        self.ws_url = "wss://wsdex.okx.com/ws/v5/dex"
        self.private_key = privat_key
        self.owner = Account.from_key(self.private_key).address
        self.ping_interval = 20  # отправлять ping каждые 20 секунд
        self.reconnect_delay = 5  # пауза перед переподключением
        self.run = False

        self.price_token_eth = 100000000
        self.price_token_base = 100000000
        self.price_token_bsc = 100000000

        self.min_sum_winsraw_eth = 0.0
        self.min_sum_winsraw_base = 0.0
        self.min_sum_winsraw_bsc = 0.0

        self.contract = self.bd.get_pair_contracts
        self.have_eth = None
        self.have_base = None
        self.have_bsc = None
    async def get_session(self) -> aiohttp.ClientSession:
        global _session
        async with _session_lock:
            if _session is None or _session.closed:
                # Создаем коннектор с настройками прокси
                connector = aiohttp.TCPConnector(
                    limit=50,
                    ttl_dns_cache=300,
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
                        total=15,
                        connect=6,
                        sock_read=6
                    ),
                    trust_env=False
                )
        return _session

    async def close_session(self):
        global _session
        async with _session_lock:
            if _session and not _session.closed:
                await _session.close()

    def _sign(self, timestamp: str) -> str:
        message = f"{timestamp}GET/users/self/verify"
        hmac_key = hmac.new(self.secret_key.encode(), message.encode(), hashlib.sha256)
        return base64.b64encode(hmac_key.digest()).decode()

    async def _login(self, ws):
        ts = str(int(time.time()))
        login_msg = {"op": "login", "args": [{
            "apiKey": self.api_key,
            "passphrase": self.passphrase,
            "timestamp": ts,
            "sign": self._sign(ts)
        }]}
        await ws.send(json.dumps(login_msg))
        res = json.loads(await ws.recv())
        # print(f'Подключение {res}')
        if res.get('event') != 'login' or res.get('code') != '0':
            raise Exception(f"Login failed: {res}")

    async def _ping_loop(self, ws):
        while True:
            try:
                await ws.send("ping")
            except Exception:
                break
            await asyncio.sleep(self.ping_interval)

    async def get_price(self, chain_index: str, token_contract: str):
        while self.run == True:
            try:
                async with websockets.connect(self.ws_url, open_timeout=30) as ws:
                    await self._login(ws)
                    # запускаем корутину пинга
                    ping_task = asyncio.create_task(self._ping_loop(ws))

                    sub_msg = {"op": "subscribe", "args": [{
                        "channel": "price",
                        "chainIndex": chain_index,
                        "tokenContractAddress": token_contract.lower()
                    }]}
                    await ws.send(json.dumps(sub_msg))

                    while True:
                        raw = await ws.recv()
                        if raw == "pong":
                            continue

                        try:
                            resp = json.loads(raw)
                        except json.JSONDecodeError:
                            print("Неожиданный формат:", raw)
                            continue
                        evt = resp.get("event")
                        if evt in ("subscribe", "channel-conn-count", "notice"):
                            continue

                        data = resp.get("data")
                        if isinstance(data, list) and data:
                            ping_task.cancel()
                            if chain_index == '1':
                                 self.price_token_eth = float(data[0]["price"])
                            if chain_index == '8453':
                                self.price_token_base = float(data[0]["price"])
                            if chain_index == '56':
                                self.price_token_bsc = float(data[0]["price"])
                            # print(f"ЦЕНА {float(data[0]["price"])}")
            except (websockets.exceptions.ConnectionClosedError, websockets.exceptions.WebSocketException, TimeoutError, OSError) as e:
                await asyncio.sleep(5)
            except Exception as e:
                await asyncio.sleep(5)
        print(f'!!!!!! {token_contract}')
        await ws.close()

    async def get_contract(self):
        return self.bd.get_pair_contracts(self.pair)

    async def is_tobe_onchain(self, chain_id, token) -> bool: #Есть ли монета на блокчейне этом или нет
        pass

    async def max_price(self, chain=None):
        # if self.price_token_eth >= self.price_token_bsc and self.price_token_eth >= self.price_token_base and self.have_eth:
        if chain is None:

            if self.have_eth and self.have_bsc is None and self.have_base is None and self.price_token_eth != 100000000:
                return self.price_token_eth, 1
            if self.have_eth and self.have_base and self.have_bsc is None and self.contract['ethereum'] != '0' and self.contract['base'] != '0':
                if self.price_token_eth >= self.price_token_base:
                    return self.price_token_eth, 1
                else:
                    return self.price_token_base, 8453
            if self.have_eth and self.have_base is None and self.have_bsc and self.contract['ethereum'] != '0' and self.contract['bsc'] != '0':
                if self.price_token_eth >= self.price_token_bsc:
                    return self.price_token_eth, 1
                else:
                    return self.price_token_bsc, 56
            if self.have_eth and self.have_base and self.have_bsc and self.contract['ethereum'] != '0' and self.contract['bsc'] != '0' and self.contract['base'] != '0':
                if self.price_token_eth >= self.price_token_bsc and self.price_token_eth >= self.price_token_base:
                    return self.price_token_eth, 1


            if self.have_base and self.have_bsc is None and self.have_eth is None:
                return self.price_token_base, 8453
            if self.have_base and self.have_bsc and self.have_eth is None and self.contract['bsc'] != '0' and self.contract['base'] != '0':
                if self.price_token_base >= self.price_token_bsc:
                    return self.price_token_base, 8453
                else:
                    return self.price_token_bsc, 56
            if self.have_base and self.have_eth and self.have_bsc and self.contract['ethereum'] != '0' and self.contract['bsc'] != '0' and self.contract['base'] != '0':
                if self.price_token_base >= self.price_token_eth and self.price_token_base >= self.price_token_bsc:
                    return self.price_token_base, 8453


            if self.have_bsc and self.have_eth is None and self.have_base is None:
                return self.price_token_bsc, 56
            if self.have_bsc and self.have_eth and self.have_base is None and self.contract['ethereum'] != '0' and self.contract['bsc']:
                if self.price_token_bsc >= self.price_token_eth:
                    return self.price_token_bsc, 56
                else:
                    return self.price_token_eth, 1
            if self.have_bsc and self.have_eth and self.have_base and self.contract['ethereum'] != '0' and self.contract['bsc'] != '0' and self.contract['base'] != '0':
                if self.price_token_bsc >= self.price_token_eth and self.price_token_bsc >= self.price_token_base:
                    return self.price_token_bsc, 56
            return 0, None
        else:
            if chain == 1 and self.price_token_eth != 100000000:
                return self.price_token_eth
            if chain == 8453:
                return self.price_token_base
            if chain == 56:
                return self.price_token_bsc

    async def min_price(self):
        if float(self.price_token_eth) <= float(self.price_token_bsc) and float(self.price_token_eth) <= float(self.price_token_base) and float(self.price_token_eth) != 0 and self.price_token_eth != 100000000:
            return float(self.price_token_eth), 1
        if float(self.price_token_base) <= float(self.price_token_eth) and float(self.price_token_base) <= float(self.price_token_bsc) and float(self.price_token_base) != 0:
            return float(self.price_token_base), 8453
        if float(self.price_token_bsc) <= float(self.price_token_eth) and float(self.price_token_bsc) <= float(self.price_token_base) and float(self.price_token_bsc) != 0:
            return float(self.price_token_bsc), 56
        return None, None

    async def generate_okx_headers(self, method: str, path: str, body: str = "") -> dict:
        """Генерация заголовков для OKX API :cite[7]"""
        timestamp = str(datetime.now(timezone.utc).isoformat(timespec="milliseconds")).replace("+00:00", "Z")
        message = timestamp + method + path + body
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

    async def swap(self, w3, chain_id, amount_from, address_from, address_to, slippage):
        try:
            session = await self.get_session()

            url = "https://www.okx.com/api/v5/dex/aggregator/swap"
            params = {
                "chainId": chain_id,
                "amount": str(float(amount_from) * (10**6)),
                "fromTokenAddress": address_from,
                "toTokenAddress": address_to,
                "slippage": str(slippage),
                "userWalletAddress": str(self.owner),
                "feePercent": "0.1",  # Примерное значение комиссии
                "toTokenReferrerAddress": str(self.owner)  # Опциональный параметр
            }

            # Формируем строку запроса для подписи
            query_string = "&".join(f"{k}={v}" for k, v in params.items())
            path_with_query = f"/api/v5/dex/aggregator/swap?{query_string}"

            headers = await self.generate_okx_headers("GET", path_with_query, body="")
            async with session.get(url, params=params, headers=headers) as resp:
                data = await resp.json()

            if data.get("code") != "0":
                raise Exception(f"Swap error: {data.get('msg')}")
            # print(f"Data swap: {data}")
            tx_info = data["data"][0]["tx"]
            token = w3.eth.contract(address=address_to, abi=ERC20_ABI)
            before = await token.functions.balanceOf(self.owner).call()
            # Формируем транзакцию
            nonce = await w3.eth.get_transaction_count(self.owner)
            transaction = {
                'to': Web3.to_checksum_address(tx_info['to']),
                'data': tx_info['data'],
                'value': int(tx_info['value']),
                'gas': int(tx_info['gas']),
                'nonce': nonce,
                'chainId': int(chain_id),
                'maxFeePerGas': int(tx_info['gasPrice']),  # Общая максимальная комиссия
                'maxPriorityFeePerGas': int(tx_info['maxPriorityFeePerGas']),
                'type': '0x2'  # Явно указываем тип EIP-1559
            }

            # Подписываем и отправляем транзакцию
            signed_tx = w3.eth.account.sign_transaction(transaction, private_key=self.private_key)#Нужно будет добавить private_key
            tx_hash = await w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            receipt = await w3.eth.wait_for_transaction_receipt(tx_hash)
            await asyncio.sleep(10)
            after = await token.functions.balanceOf(self.owner).call()
            received = float(after) - float(before)
            print(f"Получено {received}")
            return tx_hash.hex(), received
        except Exception as e:
            print(f'Ошибка в свапе: {e}')
            return False, None

    async def initialize(self):
        s = []
        # contracts = await self.get_contract()
        data = await self.exchange.fetch_deposit_withdraw_fee(self.pair.split('/')[0])
        for net in data['info']['networkList']:
            if net['network'] == "Ethereum(ERC20)":
                s.append('eth')
            if net['network'] == "BNB Smart Chain(BEP20)":
                s.append('bsc')
            if net['network'] == "BASE":
                s.append('base')
        print(s)
        if s[0] == 'eth':
            self.have_eth = True
            logging.info(f'Windrow ETH')
        if 'base' in s:
            logging.info(f'Windrow BASE')
            self.have_base = True
        if 'bsc' in s:
            logging.info(f'Windrow BSC')
            self.have_bsc = True
        # print(f'PAIR: {self.pair}')
        # if self.pair == "ZRO/USDT":
        # self.price_token_eth = get_token_price('1', contracts['ethereum'])
        # await asyncio.sleep(10)
        # self.price_token_base = get_token_price('8453', contracts['base'])
        # await asyncio.sleep(10)
        # self.price_token_bsc = get_token_price('56', contracts['bsc'])
        # await asyncio.sleep(10)
        await self.set_min_sym()
        contracts = self.bd.get_pair_contracts(self.pair)
        eth = contracts['ethereum']
        base = contracts['base']
        bsc = contracts['bsc']
        # await self.load_decimals(1, eth)
        # await self.load_decimals(56, bsc)
        # await self.load_decimals(8453, base)
        try:
            if eth != '0':
                await self.load_decimals(1, eth)
                self.price_token_eth = get_token_price('1', contracts['ethereum'])
                await asyncio.sleep(10)
                asyncio.create_task(self.get_price('1', contracts['ethereum']))
            if base != '0':
                await self.load_decimals(8453, base)
                self.price_token_base = get_token_price('8453', contracts['base'])
                await asyncio.sleep(10)
                asyncio.create_task(self.get_price('8453', contracts['base']))
            if bsc != '0':
                await self.load_decimals(56, bsc)
                self.price_token_bsc = get_token_price('56', contracts['bsc'])
                await asyncio.sleep(10)
                asyncio.create_task(self.get_price('56', contracts['bsc']))
        except Exception as e:
            if str(e) == '429 Client Error: Too Many Requests for url: https://web3.okx.com/api/v5/dex/market/price':
                print(f'Ошибка')
                await asyncio.sleep(random.randint(7, 25))
                try:
                    if eth != '0':
                        await self.load_decimals(1, eth)
                        self.price_token_eth = get_token_price('1', contracts['ethereum'])
                        await asyncio.sleep(10)
                        asyncio.create_task(self.get_price('1', contracts['ethereum']))
                    if base != '0':
                        await self.load_decimals(8453, base)
                        self.price_token_base = get_token_price('8453', contracts['base'])
                        await asyncio.sleep(10)
                        asyncio.create_task(self.get_price('8453', contracts['base']))
                    if bsc != '0':
                        await self.load_decimals(56, bsc)
                        self.price_token_bsc = get_token_price('56', contracts['bsc'])
                        await asyncio.sleep(10)
                        asyncio.create_task(self.get_price('56', contracts['bsc']))
                except Exception as e:
                    if str(e) == '429 Client Error: Too Many Requests for url: https://web3.okx.com/api/v5/dex/market/price':
                        print(f'Ошибка2')
                        await asyncio.sleep(random.randint(5, 20))
                        try:
                            if eth != '0':
                                await self.load_decimals(1, eth)
                                self.price_token_eth = get_token_price('1', contracts['ethereum'])
                                await asyncio.sleep(10)
                                asyncio.create_task(self.get_price('1', contracts['ethereum']))
                            if base != '0':
                                await self.load_decimals(8453, base)
                                self.price_token_base = get_token_price('8453', contracts['base'])
                                await asyncio.sleep(10)
                                asyncio.create_task(self.get_price('8453', contracts['base']))
                            if bsc != '0':
                                await self.load_decimals(56, bsc)
                                self.price_token_bsc = get_token_price('56', contracts['bsc'])
                                await asyncio.sleep(10)
                                asyncio.create_task(self.get_price('56', contracts['bsc']))
                        except Exception as e:
                            if str(e) == '429 Client Error: Too Many Requests for url: https://web3.okx.com/api/v5/dex/market/price':
                                print(f'Ошибка3')
                                await asyncio.sleep(random.randint(3, 20))
                                if eth != '0':
                                    await self.load_decimals(1, eth)
                                    self.price_token_eth = get_token_price('1', contracts['ethereum'])
                                    await asyncio.sleep(10)
                                    asyncio.create_task(self.get_price('1', contracts['ethereum']))
                                if base != '0':
                                    await self.load_decimals(8453, base)
                                    self.price_token_base = get_token_price('8453', contracts['base'])
                                    await asyncio.sleep(10)
                                    asyncio.create_task(self.get_price('8453', contracts['base']))
                                if bsc != '0':
                                    await self.load_decimals(56, bsc)
                                    self.price_token_bsc = get_token_price('56', contracts['bsc'])
                                    await asyncio.sleep(10)
                                    asyncio.create_task(self.get_price('56', contracts['bsc']))
                    else:
                        print(f'Unkwon Error Initialize: {e}')
            else:
                print(f'Unkwon Error Initialize: {e}')
    async def set_min_sym(self):
        r = await self.exchange.spotPrivateGetCapitalConfigGetall({'timestamp': self.exchange.milliseconds()})
        for coin in r:
            if coin['coin'] == self.pair.split('/')[0]:
                for i in coin['networkList']:
                    if i['network'] == "Ethereum(ERC20)":
                        self.min_sum_winsraw_eth = float(i['withdrawMin'])
                    if i['network'] == "BNB Smart Chain(BEP20)":
                        self.min_sum_winsraw_bsc = float(i['withdrawMin'])
                    if i['network'] == "BASE":
                        self.min_sum_winsraw_base = float(i['withdrawMin'])
                return
            break
        # for i in r[self.pair.split('/')[0]]['info']['networkList']:
        #     if i['network'] == "Ethereum(ERC20)":
        #         self.min_sum_winsraw_eth = float(i['withdrawMin'])
        #     if i['network'] == "BNB Smart Chain(BEP20)":
        #         self.min_sum_winsraw_bsc = float(i['withdrawMin'])
        #     if i['network'] == "BASE":
        #         self.min_sum_winsraw_base = float(i['withdrawMin'])

    async def load_decimals(self, chain_id: int, token_address: str):
        try:
            w3 = self.w3.get(chain_id)
            if not w3:
                raise ValueError(f"Unknown chain_id {chain_id}")
            addr = w3.to_checksum_address(token_address)
            code = await w3.eth.get_code(addr)
            if not code or code in (b'', '0x'):
                raise Exception(f"No token contract at {addr} in chain {chain_id}")
            contract = w3.eth.contract(address=addr, abi=ERC20_ABI_DECIMALS)
            try:
                # dec = await contract.functions.decimals().call()
                dec = await contract.functions.decimals().call({'from': self.owner})
                self.decimals[chain_id] = dec
            except Exception as e:
                raise Exception(f"Error calling decimals() on chain {chain_id}: {e}")
            self.decimals[chain_id] = dec
            return dec
        except Exception as e:
            print(f'Ошибка в децималс ту мени реквестс')
            if "429" in str(e):
                await asyncio.sleep(random.randint(2, 13))
                await self.load_decimals(chain_id, token_address)
            else:
                print(f'Unkwon Error Decimals: {e}')

    async def send_erc20(self, chain_id: int,
                        token_address: str, to_address: str, amount):
        try:
            w3 = self.w3[chain_id]
            account = w3.eth.account.from_key(self.private_key)
            token = w3.eth.contract(
                address=w3.to_checksum_address(token_address),
                abi=ERC20_ABI_TRANSFER
            )
            print(f'DECIMALS {self.decimals[chain_id]}, {amount}')
            # amount_units = int(amount * (10 ** self.decimals[chain_id]))
            # amount_units = int(float(amount) * (10 ** int(self.decimals[chain_id])))
            nonce = await w3.eth.get_transaction_count(account.address)
            gas_price = await w3.eth.gas_price
            print(f"SEND_ERC20: amount={amount/(10**self.decimals[chain_id])}, decimals={self.decimals[chain_id]}, amount_units={amount}")

            tx = await token.functions.transfer(
                w3.to_checksum_address(to_address),
                # amount_units
                int(amount)
            ).build_transaction({
                'from': account.address,
                'nonce': nonce,
                'gasPrice': gas_price,
                'chainId': chain_id,
            })
            signed = account.sign_transaction(tx)
            tx_hash = await w3.eth.send_raw_transaction(signed.raw_transaction)
            # print(f"🔗 TX hash: {tx_hash.hex()} в сети {chain_id}")

            receipt = await w3.eth.wait_for_transaction_receipt(tx_hash)
            print(f"✅ Receipt статус: {receipt.status}")
            return f"0x{receipt.transactionHash.hex()}"
        except Exception as e:
            print(f'Ошибка при отправке транзакции: {e}\nИнформация: cahin:{chain_id}, {token_address}, {self.pair}')
            return None

    async def approve_token(self, amount, BASE_CHAIN_ID, USDC_ADDRESS):
        """Правильное подтверждение расходов USDC"""
        # 1. Получаем адрес spender от OKX
        spender_address = await self.get_spender_address(BASE_CHAIN_ID, USDC_ADDRESS)
        w3 = self.w3[BASE_CHAIN_ID]
        # 2. Проверяем текущий allowance
        usdc_contract = w3.eth.contract(address=USDC_ADDRESS, abi=ERC20_ABI)
        current_allowance = await usdc_contract.functions.allowance(
            self.owner,
            spender_address
        ).call()
        if current_allowance >= amount:
            print(f"✅ Allowance уже достаточно: {current_allowance} >= {amount} для {BASE_CHAIN_ID}")
            return None
        # 3. Формируем транзакцию approve
        nonce = await w3.eth.get_transaction_count(self.owner)
        last_block = await w3.eth.get_block('latest')

        # Параметры газа для EIP-1559
        base_fee = last_block['baseFeePerGas']
        priority_fee = await w3.eth.max_priority_fee
        max_priority_fee_per_gas = min(priority_fee, Web3.to_wei(0.02, 'gwei'))
        max_fee_per_gas = base_fee + max_priority_fee_per_gas

        # Строим транзакцию
        tx = await usdc_contract.functions.approve(
            Web3.to_checksum_address(spender_address),
            amount
        ).build_transaction({
            'chainId': int(BASE_CHAIN_ID),
            'nonce': nonce,
            'maxFeePerGas': max_fee_per_gas,
            'maxPriorityFeePerGas': max_priority_fee_per_gas,
            'from': self.owner,
            'type': '0x2'
        })

        # Оценка газа
        try:
            gas_estimate = await usdc_contract.functions.approve(
                spender_address,
                amount
            ).estimate_gas({
                'from': self.owner,
                'nonce': nonce
            })
            tx['gas'] = gas_estimate + 10000  # Буфер 10%
        except Exception as e:
            print(f"⚠️ Ошибка оценки газа: {e}, используем 100000")
            tx['gas'] = 100000

        # 4. Подписываем и отправляем
        signed_tx = w3.eth.account.sign_transaction(tx, private_key=self.private_key)
        tx_hash = await w3.eth.send_raw_transaction(signed_tx.raw_transaction)
        receipt = await w3.eth.wait_for_transaction_receipt(tx_hash)
        print(f"Receipt статус FOR: {receipt.status}")
        return tx_hash.hex()

    async def get_spender_address(self, BASE_CHAIN_ID, USDC_ADDRESS, session):
        """Получение адреса spender (dexContractAddress) от OKX"""
        try:
            url = "https://www.okx.com/api/v5/dex/aggregator/approve-transaction"
            params = {
                "chainId": str(BASE_CHAIN_ID),
                "tokenContractAddress": str(USDC_ADDRESS),
                "approveAmount": "1000000"  # Любое значение, важно получить spender
            }
            print(f'1 {params}')
            headers = await self.generate_okx_headers("GET", "/api/v5/dex/aggregator/approve-transaction?" + "&".join(
                f"{k}={v}" for k, v in params.items()))
            async with session.get(url, params=params, headers=headers) as response:
            # response = requests.get(url, params=params, headers=headers)
                data = await response.json()
            print(f'DATA: {data}')
            if data.get("code") != "0":
                raise Exception(f"Spender error: {data.get('msg')}")

            return data["data"][0]["dexContractAddress"]
        except Exception as e:
            if str(e) == 'Spender error: Too Many Requests':
                await asyncio.sleep(random.randint(5, 12))
                res = await self.get_spender_address(BASE_CHAIN_ID, USDC_ADDRESS, session)
                return res
            else:
                print(f'Что за спендер вообще такой: {e}')
                return None