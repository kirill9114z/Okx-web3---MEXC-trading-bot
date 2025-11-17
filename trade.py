import asyncio
import time
import json
import ccxt.async_support as ccxt
from aiogram.client.session import aiohttp
from eth_account import Account
from eth_abi import decode
from web3.exceptions import ContractLogicError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pancake_trade import OkxTrade
from web3 import AsyncWeb3, Web3

from exchange import place_limit_order, get_session
from config import RPC_BSC
USDT_CONTRACTS = '0x55d398326f99059fF775485246999027B3197955'

USDC_CONTRACTS_2 = '0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d'


cached = {}
class Arbitrage:
    # def __init__(self, exchange, pair, pancakce, db, chat_id, bot, privat_key):
    def __init__(self, exchange, pair, pancakce, privat_key, address, rpc, bot, chat_id, db, max_volume):
        self.exchange = exchange
        self.pair = pair
        self.address = address
        self.pancakce = pancakce
        self.private_key = privat_key
        self.owner = Account.from_key(self.private_key)
        # self.owner = Keypair.from_base58_string(self.private_key)
        self.w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc))
        self.bot = bot
        self.chat_id = chat_id
        self.db = db

        self.running = True

        self.max_volume = max_volume
        self.balance_usdt_mexc = 200
        self.balance_token_mexc = 70
        self.balance_token_dex = 70
        self.balance_usdc_dex_bsc = 200
        self.native_token = 0


        self.Is_enough_balance_for_fee = True

        self.usdc_contracts = {}
        self._withdrawal_fee_cache = {}
        self._cache_lock = asyncio.Lock()

        self.PROFIT_THRESHOLD = 0.5
        self.GLOBAL_SPREAD = -0.1

        self.last_alert = {}
        self.alert_cooldown = 300
        self.min_profit_change = 1

        with open('pancake_router_v2_abi.json', 'r') as f:
            self.smart_router_abi = json.load(f)

        with open('erc20_abi.json', 'r') as f:
            self.erc20_abi = json.load(f)

        with open('multicall_abi.json', 'r') as f:
            self.multicall_abi = json.load(f)


        self.multicall_address = "0xcA11bde05977b3631167028862bE2a173976CA11"
        self.WBNB = Web3.to_checksum_address("0xBB4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c")
        self.tes = self.pair.split('/')
        self.symbol = f"{self.tes[0]}_{self.tes[1]}"
        self._last_fee_update = 0
        self._fee_lock = asyncio.Lock()


    def compute_prefix_stats_with_max_sum(self, order_book_levels, max_sum):
        """Вычисляет кумулятивные суммы с учетом MAX_SUM, агрегируя ордера.
        Возвращает:
            - cum_amounts: список накопленных объемов
            - cum_costs: список накопленных стоимостей
            - avg_prices: список кортежей (средняя цена, цена текущего ордера)
        """
        prices, volumes = zip(*order_book_levels) if order_book_levels else ([], [])
        cum_amounts, cum_costs, avg_prices = [], [], []
        total_amount, total_cost = 0.0, 0.0

        for price, volume in zip(prices, volumes):
            remaining = max(0, max_sum - total_cost)
            if remaining <= 0:
                break

            available_volume = min(volume, remaining / price)
            new_cost = total_cost + available_volume * price

            # Корректировка объема, если сумма превышает max_sum
            if new_cost > max_sum:
                available_volume = (max_sum - total_cost) / price
                new_cost = total_cost + available_volume * price

            # Обновляем итоговые значения
            total_amount += available_volume
            total_cost = new_cost

            # Сохраняем среднюю цену и цену текущего ордера
            avg_price = total_cost / total_amount if total_amount else price
            avg_prices.append((avg_price, price))  # Кортеж из двух значений

            cum_amounts.append(total_amount)
            cum_costs.append(total_cost)

            if total_cost >= max_sum:
                break

        return cum_amounts, cum_costs, avg_prices

    async def send_notification(self, message: str):
        if self.bot and self.chat_id:
            try:
                await self.bot.send_message(self.chat_id, message)
            except Exception as e:
                print(f"Ошибка отправки уведомления: {e}")
        print(f'SEND NOTIF: {message}')

    async def send_opportunity_alert(self, opportunity):
        """Отправляет сообщение об арбитражной возможности с кнопкой"""
        if not self.bot or not self.chat_id:
            print("Bot or chat_id not initialized. Cannot send message.")
            return

        opp_type = opportunity['type']
        volume = opportunity['volume']
        mexc_price = opportunity['mexc_price']
        okx_price = opportunity['dex_price']
        profit = opportunity['profit']
        spread = opportunity['spread']

        # Определяем направление сделки
        direction = "MEXC⬆️ → DEX⬇️" if opp_type == 'BUY_MEXC' else "OKX⬆️ → DEX⬇️"

        def format_price(p):
            if float(p) == 0:
                return "0"
            if float(p) < 0.0001:
                s = f"{p:.20f}"  # Преобразуем в строку с 20 знаками
                s_clean = s.rstrip('0').rstrip('.')  # Убираем хвостовые нули

                if '.' in s_clean and s_clean.split('.')[0] == '0':
                    fractional = s_clean.split('.')[1]
                    zeros = 0
                    # Считаем последовательные нули
                    for char in fractional:
                        if char == '0':
                            zeros += 1
                        else:
                            break
                    # Если нулей >=5 и есть значащие цифры
                    if zeros >= 4 and zeros < len(fractional):
                        return f"0.{{{zeros}}}{fractional[zeros:zeros + 8]}"  # Берем до 8 значащих цифр
                return f"{s_clean:.8f}".rstrip('0')
            else:
                # Для обычных цен убираем лишние нули
                return f"{p:.8f}".rstrip('0')

        mexc_price_str = format_price(mexc_price)
        okx_price_str = format_price(okx_price)

        # Создаем текст сообщения
        message_text = (
            f"🚀 *Арбитражная возможность!*\n\n"
            f"*Направление:* {direction}\n"
            f"*Пара:* {self.pair}\n"
            f"*Объем:* {volume:.6f}\n"
            f"*Цена MEXC:* {mexc_price_str}\n"
            f"*Цена OKX:* {okx_price_str}\n"
            f"*Прибыль:* ${profit:.4f}\n"
            f"*Спред:* {spread:.4f}%\n"
        )

        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=message_text,
                parse_mode="Markdown",
            )
        except Exception as e:
            print(f"Error sending message: {e}")


    # async def _safe_fetch_balance(self, max_retries=3, delay=5):
    #     """Безопасное получение баланса с MEXC с повторными попытками"""
    #     for attempt in range(max_retries):
    #         try:
    #             balance = await self.exchange.fetch_balance()
    #             if 'USDT' in balance['total'] and f'{self.pair.split("/")[0]}' in balance['total']:
    #                 # return float(balance['total']['USDT']), float(balance['total'][self.pair.split("/")[0]])
    #                 return 2, 4
    #             return 2, 3
    #         except (ccxt.RequestTimeout, ccxt.NetworkError) as e:
    #             if attempt + 1 < max_retries:
    #                 await asyncio.sleep(delay)
    #         except Exception as e:
    #             print(f'ERROR SAFE_FETCH: {e}')
    #             break
    #     return 0.0
    #
    # async def _safe_get_bnb_balance(self, max_retries=3, delay=5):
    #     """Получение баланса нативной монеты BNB"""
    #     for attempt in range(1, max_retries + 1):
    #         try:
    #             # Получаем баланс напрямую через web3
    #             raw_balance = await self.w3.eth.get_balance(
    #                 self.w3.to_checksum_address(self.owner.address)
    #             )
    #             return raw_balance / (10 ** 18)  # BNB имеет 18 десятичных знаков
    #
    #         except Exception as e:
    #             print(f"RPC error in (attempt {attempt}): {e}")
    #             if attempt < max_retries:
    #                 await asyncio.sleep(delay)
    #
    #     return 0.0
    #
    # async def _safe_get_erc20_balance(self, address, decimals, max_retries=3, delay=5):
    #     print(f' Addres: {self.owner.address}')
    #     addr = self.w3.to_checksum_address(address)
    #     contract = self.w3.eth.contract(
    #         address=addr,
    #         abi=self.erc20_abi
    #     )
    #     # 3) Цикл повторов
    #     for attempt in range(1, max_retries + 1):
    #         try:
    #             # вызвать асинхронно .call()
    #             raw: int = await contract.functions.balanceOf(
    #                 self.w3.to_checksum_address(self.owner.address)
    #             ).call()
    #             print(f' RES : {raw}  |  {raw/10**18}')
    #             return (raw / (10**decimals))
    #             # return (raw / (10**18))
    #
    #         except ContractLogicError as e:
    #             print(f"Contract error: {e}")
    #             break  # при ошибке контракта повторять не нужно
    #
    #         except Exception as e:
    #             print(f"RPC error in (attempt {attempt}): {e}")
    #             if attempt < max_retries:
    #                 await asyncio.sleep(delay)
    #
    #     return 0.0
    #
    # async def update_balances(self):
    #     """Обновляет все балансы параллельно"""
    #     try:
    #         t = time.time()
    #         # Создаем задачи для всех балансов
    #         tasks = [
    #             self._get_mexc_balances(),
    #             self._safe_get_bnb_balance(),
    #             self._get_dex_balances(18)
    #         ]
    #
    #         # Выполняем все задачи параллельно
    #         results = await asyncio.gather(*tasks, return_exceptions=True)
    #
    #         # Обрабатываем результаты
    #         mexc_balances, bnb_balance, dex_balances = results
    #
    #         # Устанавливаем балансы MEXC
    #         self.balance_usdt_mexc, self.balance_token_mexc = mexc_balances
    #         self.native_token = bnb_balance
    #         self.balance_token_dex, self.balance_usdc_dex_bsc = dex_balances
    #         print(self.balance_usdt_mexc, self.balance_token_mexc)
    #         print(self.native_token)
    #         print(self.balance_token_dex, self.balance_usdc_dex_bsc)
    #         print(f'TIME: {time.time() - t}')
    #         return True
    #
    #     except Exception as e:
    #         print(f"Failed to update balances: {e}")
    #         return False
    #
    # async def _get_mexc_balances(self):
    #     """Получает балансы с MEXC"""
    #     usdt_balance, token_balance = await self._safe_fetch_balance()
    #     return usdt_balance, token_balance
    #
    #
    # async def _get_dex_balances(self, token_decimals):
    #     """Получает балансы токенов на DEX параллельно"""
    #     # Запускаем запросы балансов параллельно
    #     token_task = self._safe_get_erc20_balance("0xaa036928c9c0Df07d525B55ea8EE690Bb5a628C1", token_decimals)
    #     usdc_task = self._safe_get_erc20_balance(USDT_CONTRACTS, 18)
    #
    #     token_balance, usdc_balance = await asyncio.gather(token_task, usdc_task)
    #     return token_balance, usdc_balance





    # async def update_balances(self, stale_seconds: int = 3):
    #     try:
    #         t0 = time.time()
    #
    #         token_addresses = [
    #             "0xaa036928c9c0Df07d525B55ea8EE690Bb5a628C1",  # ваш токен
    #             USDT_CONTRACTS
    #         ]
    #         decimals_map = {
    #             token_addresses[0]: 18,
    #             token_addresses[1]: 18
    #         }
    #
    #         task_exchange = asyncio.create_task(self._safe_fetch_balance())
    #         task_multicall = asyncio.create_task(
    #             self._multicall_balances_with_native(token_addresses, self.owner.address, decimals_map)
    #         )
    #
    #         mexc_res, multicall_res = await asyncio.gather(task_exchange, task_multicall, return_exceptions=True)
    #
    #         if isinstance(mexc_res, Exception):
    #             print("MEXC task error:", mexc_res)
    #             mexc_res = (0.0, 0.0)
    #
    #         if isinstance(multicall_res, Exception):
    #             print("Multicall task error:", multicall_res)
    #             token_dict = {addr: 0.0 for addr in token_addresses}
    #             native_val = await self._safe_get_bnb_balance()
    #         else:
    #             token_dict, native_val = multicall_res
    #             if native_val is None:
    #                 native_val = await self._safe_get_bnb_balance()
    #
    #         self.balance_usdt_mexc, self.balance_token_mexc = mexc_res
    #         self.native_token = native_val
    #         self.balance_token_dex = token_dict.get(token_addresses[0], 0.0)
    #         self.balance_usdc_dex_bsc = token_dict.get(token_addresses[1], 0.0)
    #
    #         print(f"MEXC {self.balance_usdt_mexc, self.balance_token_mexc}")
    #         print(f"Native: {self.native_token}")
    #         print(f"Dex: {self.balance_token_dex, self.balance_usdc_dex_bsc}")
    #         print(f"TIME: {time.time() - t0}")
    #
    #         return True
    #     except Exception as e:
    #         print(f"Failed to update balances: {e}")
    #         return False


    async def update_balances(self, stale_seconds: int = 3):
        try:
            self.balance_usdt_mexc, self.balance_token_mexc = 1000, 800
            self.native_token = 0.1
            self.balance_token_dex = 1000
            self.balance_usdc_dex_bsc = 800
            return True
        except Exception as e:
            print(f"Failed to update balances: {e}")
            return False


    async def _safe_fetch_balance(self, max_retries: int = 2, delay: float = 0.5):
        """
        Получить балансы с биржи (USDT и токен).
        """
        for attempt in range(1, max_retries + 1):
            try:
                # таймаут на случай зависания
                res = await asyncio.wait_for(self.exchange.fetch_balance(), timeout=6)
                base_symbol = self.pair.split("/")[0]
                usdt = float(res['total'].get('USDT', 0))
                token = float(res['total'].get(base_symbol, 0))
                return usdt, token
            except asyncio.TimeoutError:
                print(f"fetch_balance timeout attempt {attempt}")
            except Exception as e:
                print(f"ERROR SAFE_FETCH attempt {attempt}: {e}")
            if attempt < max_retries:
                await asyncio.sleep(delay)
        return 0.0, 0.0

    async def _safe_get_bnb_balance(self, max_retries: int = 3, delay: float = 1.0):
        """
        Получить баланс нативной монеты (BNB).
        """
        for attempt in range(1, max_retries + 1):
            try:
                raw = await asyncio.wait_for(
                    self.w3.eth.get_balance(self.w3.to_checksum_address(self.owner.address)),
                    timeout=5
                )
                return raw / (10 ** 18)
            except Exception as e:
                print(f"RPC error in BNB (attempt {attempt}): {e}")
            if attempt < max_retries:
                await asyncio.sleep(delay)
        return 0.0

    async def _multicall_balances_with_native(self,
                                              token_addresses: list,
                                              wallet_address: str,
                                              token_decimals_map: dict):
        mc = self.w3.eth.contract(
            address=self.w3.to_checksum_address(self.multicall_address),
            abi=self.multicall_abi
        )

        wallet_addr = self.w3.to_checksum_address(wallet_address)
        calls = []

        # токены
        for ta in token_addresses:
            token_contract = self.w3.eth.contract(
                address=self.w3.to_checksum_address(ta),
                abi= self.erc20_abi
            )
            call_data = token_contract.encode_abi(
                abi_element_identifier='balanceOf',
                args=[wallet_addr]
            )
            calls.append((self.w3.to_checksum_address(ta), call_data))

        calls.append((wallet_addr, "0x"))

        try:
            result = await mc.functions.aggregate(calls).call()
            # _, return_data_list = result
            return_data_list = result[1]
        except Exception as e:
            print(f"Multicall failed (with native) : {e}")
            # fallback
            token_vals = await asyncio.gather(
                *(self._safe_get_erc20_balance(addr, token_decimals_map.get(addr, 18))
                  for addr in token_addresses)
            )
            native = await self._safe_get_bnb_balance()
            return dict(zip(token_addresses, token_vals)), native

        balances = {}
        native_balance = None

        # парсим токены
        for idx, addr in enumerate(token_addresses):
            raw_bytes = return_data_list[idx]
            try:
                # преобразование
                if isinstance(raw_bytes, str) and raw_bytes.startswith("0x"):
                    data_bytes = bytes.fromhex(raw_bytes[2:])
                else:
                    data_bytes = raw_bytes
                raw_int = decode(['uint256'], data_bytes)[0]
                decimals = token_decimals_map.get(addr, 18)
                balances[addr] = raw_int / (10 ** decimals)
            except Exception as e:
                print(f"Failed parse token {addr}: {e}")
                balances[addr] = 0.0

        # парсим нативку (последний)
        # raw_native_bytes = return_data_list[len(token_addresses)]
        # try:
        #     if isinstance(raw_native_bytes, str) and raw_native_bytes.startswith("0x"):
        #         data_bytes = bytes.fromhex(raw_native_bytes[2:])
        #     else:
        #         data_bytes = raw_native_bytes
        #     print(f'Nat: {data_bytes}\n\n{raw_native_bytes}\n{token_addresses}')
        #     native_int = decode(['uint256'], data_bytes)[0]
        #     print(f'Res nat: {native_int / (10 ** 18)}')
        #     native_balance = native_int / (10 ** 18)
        # except Exception as e:
        #     print(f"Failed parse native balance: {e}")
        #     native_balance = None
        # Альтернатива: получить нативный баланс отдельным вызовом
        native_balance = await self.w3.eth.get_balance(wallet_addr) / (10 ** 18)
        return balances, native_balance

    async def _safe_get_erc20_balance(self, address: str, decimals: int,
                                      max_retries: int = 3, delay: float = 1.0):
        """
        Индивидуальный вызов баланса ERC-20. Используется как fallback.
        """
        addr = self.w3.to_checksum_address(address)
        contract = self.w3.eth.contract(address=addr, abi=self.erc20_abi)
        for attempt in range(1, max_retries + 1):
            try:
                raw = await asyncio.wait_for(
                    contract.functions.balanceOf(self.w3.to_checksum_address(self.owner.address)).call(),
                    timeout=5
                )
                return raw / (10 ** decimals)
            except asyncio.TimeoutError:
                print(f"ERC20 balance timeout attempt {attempt} for {address}")
            except Exception as e:
                print(f"RPC error ERC20 (attempt {attempt}) for {address}: {e}")
            if attempt < max_retries:
                await asyncio.sleep(delay)
        return 0.0


    async def get_price_mexc(self, session):
        # session = await get_session()
        u_id = self.db.get_uid(self.pair)
        if session is None:
            session = await get_session()
        # u_id = self.db.get_uid()
        headers = {
            "Referer": f"https://www.mexc.com/exchange/{self.symbol}",
            "Cookie": f"uc_token={u_id}; u_id={u_id};",
            "X-Requested-With": "XMLHttpRequest",
        }
        ask = []
        bids = []
        try:
            # params = {"symbol": str(self.symbol), "type": "step0"}
            params = {"symbol": str(self.symbol)}
            if session is not None:
                async with session.get(f"https://www.mexc.com/api/platform/spot/market/depth", headers=headers, params=params, timeout=5) as resp:
                    data = await resp.json()
                    k = 0
                    for i in data['data']['data']['asks']:
                        ask.append([float(i['p']), float(i['q'])])
                        k += 1
                        if k == 7:
                            break
                    b = 0
                    for i in data['data']['data']['bids']:
                        bids.append([float(i['p']), float(i['q'])])
                        b += 1
                        if b == 7:
                            break
            else:
                return None, None, None, None, None, None
        # except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        #     print(f"Error fetching vcoinId get_price: {e}")
        #     return None, None, None, None, None, None
        except Exception as e:
            if str(e) == 'Session is closed':
                return None, None, None, None, None, None
            if str(e) == "'data'":
                await self.send_notification(f'Нет пары на MEXC: {self.pair}\nОстановите скрипт и удалите пару')
                return None, None, None, None, None, None
            print(f'UNKNOWERROR get_price: {e} ')
            return None, None, None, None, None, None
        ask_amounts, ask_costs, ask_avg = self.compute_prefix_stats_with_max_sum(ask, self.max_volume if self.max_volume is not None else self.balance_usdt_mexc)
        bid_amounts, bid_costs, bid_avg = self.compute_prefix_stats_with_max_sum(bids, self.max_volume if self.max_volume is not None else self.balance_usdc_dex_bsc)
        return ask_amounts, ask_costs, ask_avg, bid_amounts, bid_costs, bid_avg





    async def _calc_buy_mecx_fee(self, volume, price):
        fee_mexc = float(volume) * float(price) * 0.0005
        fee_panckake = float(volume) * float(price) * 0.00025
        fee = fee_mexc + fee_panckake + 0.01
        return fee

    async def analyze_opportunities(self):
        print(f'Start 1')
        session = await get_session()
        if session is None:
            await asyncio.sleep(3)
            session = await get_session()
            if session is None:
                session = await get_session()

        spread = self.db.get_pair_spread(self.pair)
        curr_spread = spread if spread else self.db.get_global_spread()
        try:
            while self.running == True:
                t = time.time()
                # Получаем данные стакана с MEXC
                ask_amounts, ask_costs, ask_avg, bid_amounts, bid_costs, bid_avg = await self.get_price_mexc(session)
                if ask_amounts is None:
                    continue
                # Используем максимальную цену OKX как эталон для продажи
                okx_sell_price = self.pancakce.sell
                if okx_sell_price == 0:
                    continue

                candidates = []
                for i in range(len(ask_amounts)):
                    volume = ask_amounts[i] if ((ask_amounts[i] * ask_avg[i][0]) <= self.balance_usdt_mexc) else (self.balance_usdt_mexc/ask_avg[i][0])
                    mexc_price = ask_avg[i][0]
                    price = ask_avg[i][1]
                    # Рассчитываем комиссии и проскальзывание для OKX
                    # okx_effective_price = float(okx_sell_price) * 0.99
                    okx_effective_price = float(okx_sell_price)

                    # Рассчитываем прибыль
                    profit = (okx_effective_price - mexc_price) * volume
                    if (float(volume) * float(mexc_price) <= self.max_volume) and float(volume) <= float(self.balance_token_dex):
                        spread = ((okx_effective_price - mexc_price) / mexc_price) * 100
                        if float(spread) >= float(curr_spread):                  #СПРЕД !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
                            candidates.append({
                                'type': 'BUY_MEXC',
                                'volume': volume,
                                'mexc_price': mexc_price,
                                'dex_price': okx_effective_price,
                                'price': price,
                                'profit': profit,
                                'spread': spread,
                                'level': i + 1,
                                'time': time.time() - t
                            })

                okx_buy_price = self.pancakce.buy
                if okx_buy_price == 0:
                    continue

                # Анализируем BIDS (покупка на OKX -> продажа на MEXC)
                for i in range(len(bid_amounts)):
                    volume = bid_amounts[i] if ((bid_amounts[i] * okx_buy_price) <= self.balance_usdc_dex_bsc) else (self.balance_usdc_dex_bsc / okx_buy_price)
                    mexc_price = bid_avg[i][0]  # Средняя цена продажи на MEXC
                    price = bid_avg[i][1]
                    # Используем минимальную цену OKX для покупки
                    okx_effective_price = float(okx_buy_price)

                    # Рассчитываем прибыль
                    profit = (mexc_price - okx_effective_price) * volume
                    if (float(okx_buy_price) * float(volume) <= self.max_volume) and (volume <= self.balance_token_mexc):
                        spread = ((mexc_price - okx_effective_price) / okx_effective_price) * 100
                        if float(spread) >= float(curr_spread):       #СПРЕД !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
                            candidates.append({
                                'type': 'SELL_MEXC',
                                'volume': volume,
                                'mexc_price': mexc_price,
                                'dex_price': okx_effective_price,
                                'price': price,
                                'profit': profit,
                                'spread': spread,
                                'level': i+1,
                                'time': time.time() - t
                            })

                if candidates:
                    best = max(candidates, key=lambda x: x['profit'])
                    # print(f'10: {best}')
                    # fee = calculate_total_gas_cost(id3[best['chain_id']], cureent, best['volume'])
                    # best['profit'] = best['profit'] - fee
                    # print(f'1: {best}')
                    fee = await self._calc_buy_mecx_fee(best['volume'], best['mexc_price'])
                    if best['type'] == 'BUY_MEXC':
                        best['profit'] -= float(fee)
                        if best['profit'] >= self.PROFIT_THRESHOLD:
                            print(f'BUY {best} | {fee}')
                            await self.send_opportunity_alert(best)
                            # await self.make_trade(best, session)
                    else:
                        best['profit'] -= float(fee)
                        if best['profit'] >= self.PROFIT_THRESHOLD:
                            print(f'SEll | {best} | {fee}')
                            await self.send_opportunity_alert(best)
                            # await self.make_trade(best, session)
                else:
                    continue
        except Exception as e:
            self.running = False
            await self.send_notification(f'Произошла ошибка: {e}. Перезапуск через 30 сек')
            await asyncio.sleep(5)
            self.running = True
        # finally:
        # await session.close()

    async def make_trade(self, best, session):
        curr_pair = self.pair.split('/')
        u_id = self.db.get_uid(self.pair)
        try:
            if best['type'] == 'SELL_MEXC':
                order = await place_limit_order(curr_pair, best['price'], best['volume'], True, u_id, session)
                if order == False:
                    await self.send_notification('Скрипт остановлен, U_id токен устарел, нажмите настройки и поменяйте на новый')
                    self.running = False
                print(f"ОРДЕРД {order}")
                if order:
                    order_id = order['data']
                    tim = time.time()
                    while True:
                        status = await self.exchange.fetch_order(order_id, self.pair)
                        if time.time() - tim >= 1 and status['status'] == 'open' and status['filled'] == 0:
                            await self.exchange.cancel_order(order_id, self.pair)
                            return
                        if time.time() - tim >= 1 and status['status'] == 'open' and status['filled'] > 0:
                            await self.exchange.cancel_order(order_id, self.pair)
                            val = await self.pancakce.swap_universal_async(USDT_CONTRACTS, self.address, best['dex'] * status['filled'])
                            if val == 'прошла обратная замена КУПИЛИ ЗАНОВО':
                                await self.send_notification(
                                    f'Не получилось сделать транзакцию на Pancake, совершите сделку самостоятельно на dex:\nКупите на {best['dex'] * status['filled']}$ монет {self.pair.split('/')[0]} за 5 мин')
                                await asyncio.sleep(300)
                                await self.update_balances()
                                return
                            else:
                                print(f'ВСЕ ЗАКОНЧИЛОСЬ {val}')
                                notification_text = (
                                    f"🔔 Новая сделка!\n"
                                    f"Тип: {'Продажа' if best['type'] == 'SELL_MEXC' else 'Покупка'}\n"
                                    f"Объем: {status['filled']:.2f}\n"
                                    f"Прибыль: ${(status['filled'] / best["amount"]) * best['profit']:.2f}\n"
                                    f"Хэш: {val}"
                                )
                                await self.send_notification(notification_text)
                                await self.update_balances()
                                return
                        if status['status'] == 'closed':
                            print(status)
                            break
                        if status['status'] == "canceled":
                            val = await self.pancakce.swap_universal_async(USDT_CONTRACTS, self.address, best['dex'] * status['filled'])
                            if val == 'прошла обратная замена КУПИЛИ ЗАНОВО':
                                await self.send_notification(
                                    f'Не получилось сделать транзакцию на Pancake, совершите сделку самостоятельно на dex:\nКупите на {best['dex'] * status['filled']}$ монет {self.pair.split('/')[0]} за 5 мин')
                                await asyncio.sleep(300)
                                await self.update_balances()
                                return
                            else:
                                print(f'ВСЕ ЗАКОНЧИЛОСЬ {val}')
                                notification_text = (
                                    f"🔔 Новая сделка!\n"
                                    f"Тип: {'Продажа' if best['type'] == 'SELL_MEXC' else 'Покупка'}\n"
                                    f"Объем: {status['filled']:.2f}\n"
                                    f"Прибыль: ${(status['filled'] / best["amount"]) * best['profit']:.2f}\n"
                                    f"Хэш: {val}"
                                )
                                await self.send_notification(notification_text)
                                await self.update_balances()
                                return
                        await asyncio.sleep(0.05)
                    # 3) DEX swap
                    val = await self.pancakce.swap_universal_async(USDT_CONTRACTS, self.address, best['dex'] * status['filled'])
                    if val == 'прошла обратная замена КУПИЛИ ЗАНОВО':
                        await self.send_notification(
                            f'Не получилось сделать транзакцию на Pancake, совершите сделку самостоятельно на dex:\nКупите на {best['dex'] * status['filled']}$ монет {self.pair.split('/')[0]} за 5 мин')
                        await asyncio.sleep(300)
                        await self.update_balances()
                        return
                    else:
                        print(f'ВСЕ ЗАКОНЧИЛОСЬ {val}')
                        notification_text = (
                            f"🔔 Новая сделка!\n"
                            f"Тип: {'Продажа' if best['type'] == 'SELL_MEXC' else 'Покупка'}\n"
                            f"Объем: {best['amount']:.2f}\n"
                            f"Прибыль: ${best['profit']:.2f}\n"
                            f"Хэш: {val}"
                        )
                        await self.send_notification(notification_text)
                        await self.update_balances()
                        return
            else:
                order = await place_limit_order(curr_pair, best["price"], best['volume'], False, u_id, session)
                if order == False:
                    await self.send_notification(
                        'Скрипт остановлен, U_id токен устарел, нажмите настройки и поменяйте на новый')
                    self.running = False
                print(f"ОРДЕРД {order}")
                if order:
                    order_id = order['data']
                    tim = time.time()
                    while True:
                        status = await self.exchange.fetch_order(order_id, self.pair)
                        if time.time() - tim >= 1 and status['status'] == 'open' and status['filled'] == 0:
                            await self.exchange.cancel_order(order_id, self.pair)
                            return
                        if time.time() - tim >= 1 and status['status'] == 'open' and status['filled'] > 0:
                            await self.exchange.cancel_order(order_id, self.pair)
                            val = await self.pancakce.swap_universal_async(self.address, USDT_CONTRACTS, status['filled'])
                            if val == 'прошла обратная замена КУПИЛИ ЗАНОВО':
                                await self.send_notification(
                                    f'Не получилось сделать транзакцию, Продайте {status['filled']} {self.pair.split("/")[0]} в течении 5 минут')
                                await asyncio.sleep(300)
                                await self.update_balances()
                                return
                            else:
                                print(f'ВСЕ ЗАКОНЧИЛОСЬ {val}')
                                notification_text = (
                                    f"🔔 Новая сделка!\n"
                                    f"Тип: {'Продажа' if best['type'] == 'SELL_MEXC' else 'Покупка'}\n"
                                    f"Объем: {status['filled']:.2f}\n"
                                    f"Прибыль: ${(status['filled'] / best["amount"]) * best['profit']:.2f}\n"
                                    f"Хэш: {val}"
                                )
                                await self.send_notification(notification_text)
                                await self.update_balances()
                                return
                        if status['status'] == 'closed':
                            print(status)
                            break
                        if status['status'] == "canceled":
                            val = await self.pancakce.swap_universal_async(self.address, USDT_CONTRACTS, status['filled'])
                            if val == 'прошла обратная замена КУПИЛИ ЗАНОВО':
                                await self.send_notification(
                                    f'Не получилось сделать транзакцию, Продайте {status['filled']} {self.pair.split("/")[0]} в течении 5 минут')
                                await asyncio.sleep(300)
                                await self.update_balances()
                                return
                            else:
                                print(f'ВСЕ ЗАКОНЧИЛОСЬ {val}')
                                notification_text = (
                                    f"🔔 Новая сделка!\n"
                                    f"Тип: {'Продажа' if best['type'] == 'SELL_MEXC' else 'Покупка'}\n"
                                    f"Объем: {status['filled']:.2f}\n"
                                    f"Прибыль: ${(status['filled'] / best["amount"]) * best['profit']:.2f}\n"
                                    f"Хэш: {val}"
                                )
                                await self.send_notification(notification_text)
                                await self.update_balances()
                                return
                        await asyncio.sleep(0.05)
                    # 3) DEX swap
                    val = await self.pancakce.swap_universal_async(self.address, USDT_CONTRACTS, status['filled'])
                    if val == 'прошла обратная замена КУПИЛИ ЗАНОВО':
                        await self.send_notification(f'Не получилось сделать транзакцию, Продайте {status['filled']} {self.pair.split("/")[0]} в течении 5 минут')
                        await asyncio.sleep(300)
                        await self.update_balances()
                        return
                    else:
                        print(f'ВСЕ ЗАКОНЧИЛОСЬ {val}')
                        notification_text = (
                            f"🔔 Новая сделка!\n"
                            f"Тип: {'Продажа' if best['type'] == 'SELL_MEXC' else 'Покупка'}\n"
                            f"Объем: {best['amount']:.2f}\n"
                            f"Прибыль: ${best['profit']:.2f}\n"
                            f"Хэш: {val}"
                        )
                        await self.send_notification(notification_text)
                        await self.update_balances()
                        return
        except Exception as e:
            print(f'Error in make_trade: {e}')

async def main():
    global mexc_client
    from config import  API_KEY_MEXC, API_SECRET_MEXC
    from database import Database
    try:
        mexc_client = ccxt.mexc({
            'apiKey': API_KEY_MEXC,
            'secret': API_SECRET_MEXC,  # НУЖНО БУДЕТ ИМПОРТИРОВАТЬ ИЗ КОНФИГА АПИ_КЕЙ И АПИСИКРЕТ
            'enableRateLimit': True,
            'timeout': 30000
        })
        db = Database()
        rpc = "https://bnb-mainnet.g.alchemy.com/v2/aedh6t876rEucrZvxICcO"
        wss = 'wss://bnb-mainnet.g.alchemy.com/v2/HrpyTKu0jGPtMyZvD5iCV'
        abi =[{"inputs":[],"stateMutability":"nonpayable","type":"constructor"},{"anonymous":False,"inputs":[{"indexed":True,"internalType":"address","name":"owner","type":"address"},{"indexed":True,"internalType":"int24","name":"tickLower","type":"int24"},{"indexed":True,"internalType":"int24","name":"tickUpper","type":"int24"},{"indexed":False,"internalType":"uint128","name":"amount","type":"uint128"},{"indexed":False,"internalType":"uint256","name":"amount0","type":"uint256"},{"indexed":False,"internalType":"uint256","name":"amount1","type":"uint256"}],"name":"Burn","type":"event"},{"anonymous":False,"inputs":[{"indexed":True,"internalType":"address","name":"owner","type":"address"},{"indexed":False,"internalType":"address","name":"recipient","type":"address"},{"indexed":True,"internalType":"int24","name":"tickLower","type":"int24"},{"indexed":True,"internalType":"int24","name":"tickUpper","type":"int24"},{"indexed":False,"internalType":"uint128","name":"amount0","type":"uint128"},{"indexed":False,"internalType":"uint128","name":"amount1","type":"uint128"}],"name":"Collect","type":"event"},{"anonymous":False,"inputs":[{"indexed":True,"internalType":"address","name":"sender","type":"address"},{"indexed":True,"internalType":"address","name":"recipient","type":"address"},{"indexed":False,"internalType":"uint128","name":"amount0","type":"uint128"},{"indexed":False,"internalType":"uint128","name":"amount1","type":"uint128"}],"name":"CollectProtocol","type":"event"},{"anonymous":False,"inputs":[{"indexed":True,"internalType":"address","name":"sender","type":"address"},{"indexed":True,"internalType":"address","name":"recipient","type":"address"},{"indexed":False,"internalType":"uint256","name":"amount0","type":"uint256"},{"indexed":False,"internalType":"uint256","name":"amount1","type":"uint256"},{"indexed":False,"internalType":"uint256","name":"paid0","type":"uint256"},{"indexed":False,"internalType":"uint256","name":"paid1","type":"uint256"}],"name":"Flash","type":"event"},{"anonymous":False,"inputs":[{"indexed":False,"internalType":"uint16","name":"observationCardinalityNextOld","type":"uint16"},{"indexed":False,"internalType":"uint16","name":"observationCardinalityNextNew","type":"uint16"}],"name":"IncreaseObservationCardinalityNext","type":"event"},{"anonymous":False,"inputs":[{"indexed":False,"internalType":"uint160","name":"sqrtPriceX96","type":"uint160"},{"indexed":False,"internalType":"int24","name":"tick","type":"int24"}],"name":"Initialize","type":"event"},{"anonymous":False,"inputs":[{"indexed":False,"internalType":"address","name":"sender","type":"address"},{"indexed":True,"internalType":"address","name":"owner","type":"address"},{"indexed":True,"internalType":"int24","name":"tickLower","type":"int24"},{"indexed":True,"internalType":"int24","name":"tickUpper","type":"int24"},{"indexed":False,"internalType":"uint128","name":"amount","type":"uint128"},{"indexed":False,"internalType":"uint256","name":"amount0","type":"uint256"},{"indexed":False,"internalType":"uint256","name":"amount1","type":"uint256"}],"name":"Mint","type":"event"},{"anonymous":False,"inputs":[{"indexed":False,"internalType":"uint32","name":"feeProtocol0Old","type":"uint32"},{"indexed":False,"internalType":"uint32","name":"feeProtocol1Old","type":"uint32"},{"indexed":False,"internalType":"uint32","name":"feeProtocol0New","type":"uint32"},{"indexed":False,"internalType":"uint32","name":"feeProtocol1New","type":"uint32"}],"name":"SetFeeProtocol","type":"event"},{"anonymous":False,"inputs":[{"indexed":False,"internalType":"address","name":"addr","type":"address"}],"name":"SetLmPoolEvent","type":"event"},{"anonymous":False,"inputs":[{"indexed":True,"internalType":"address","name":"sender","type":"address"},{"indexed":True,"internalType":"address","name":"recipient","type":"address"},{"indexed":False,"internalType":"int256","name":"amount0","type":"int256"},{"indexed":False,"internalType":"int256","name":"amount1","type":"int256"},{"indexed":False,"internalType":"uint160","name":"sqrtPriceX96","type":"uint160"},{"indexed":False,"internalType":"uint128","name":"liquidity","type":"uint128"},{"indexed":False,"internalType":"int24","name":"tick","type":"int24"},{"indexed":False,"internalType":"uint128","name":"protocolFeesToken0","type":"uint128"},{"indexed":False,"internalType":"uint128","name":"protocolFeesToken1","type":"uint128"}],"name":"Swap","type":"event"},{"inputs":[{"internalType":"int24","name":"tickLower","type":"int24"},{"internalType":"int24","name":"tickUpper","type":"int24"},{"internalType":"uint128","name":"amount","type":"uint128"}],"name":"burn","outputs":[{"internalType":"uint256","name":"amount0","type":"uint256"},{"internalType":"uint256","name":"amount1","type":"uint256"}],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"address","name":"recipient","type":"address"},{"internalType":"int24","name":"tickLower","type":"int24"},{"internalType":"int24","name":"tickUpper","type":"int24"},{"internalType":"uint128","name":"amount0Requested","type":"uint128"},{"internalType":"uint128","name":"amount1Requested","type":"uint128"}],"name":"collect","outputs":[{"internalType":"uint128","name":"amount0","type":"uint128"},{"internalType":"uint128","name":"amount1","type":"uint128"}],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"address","name":"recipient","type":"address"},{"internalType":"uint128","name":"amount0Requested","type":"uint128"},{"internalType":"uint128","name":"amount1Requested","type":"uint128"}],"name":"collectProtocol","outputs":[{"internalType":"uint128","name":"amount0","type":"uint128"},{"internalType":"uint128","name":"amount1","type":"uint128"}],"stateMutability":"nonpayable","type":"function"},{"inputs":[],"name":"factory","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"fee","outputs":[{"internalType":"uint24","name":"","type":"uint24"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"feeGrowthGlobal0X128","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"feeGrowthGlobal1X128","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"address","name":"recipient","type":"address"},{"internalType":"uint256","name":"amount0","type":"uint256"},{"internalType":"uint256","name":"amount1","type":"uint256"},{"internalType":"bytes","name":"data","type":"bytes"}],"name":"flash","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"uint16","name":"observationCardinalityNext","type":"uint16"}],"name":"increaseObservationCardinalityNext","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"uint160","name":"sqrtPriceX96","type":"uint160"}],"name":"initialize","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[],"name":"liquidity","outputs":[{"internalType":"uint128","name":"","type":"uint128"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"lmPool","outputs":[{"internalType":"contract IPancakeV3LmPool","name":"","type":"address"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"maxLiquidityPerTick","outputs":[{"internalType":"uint128","name":"","type":"uint128"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"address","name":"recipient","type":"address"},{"internalType":"int24","name":"tickLower","type":"int24"},{"internalType":"int24","name":"tickUpper","type":"int24"},{"internalType":"uint128","name":"amount","type":"uint128"},{"internalType":"bytes","name":"data","type":"bytes"}],"name":"mint","outputs":[{"internalType":"uint256","name":"amount0","type":"uint256"},{"internalType":"uint256","name":"amount1","type":"uint256"}],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"uint256","name":"","type":"uint256"}],"name":"observations","outputs":[{"internalType":"uint32","name":"blockTimestamp","type":"uint32"},{"internalType":"int56","name":"tickCumulative","type":"int56"},{"internalType":"uint160","name":"secondsPerLiquidityCumulativeX128","type":"uint160"},{"internalType":"bool","name":"initialized","type":"bool"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"uint32[]","name":"secondsAgos","type":"uint32[]"}],"name":"observe","outputs":[{"internalType":"int56[]","name":"tickCumulatives","type":"int56[]"},{"internalType":"uint160[]","name":"secondsPerLiquidityCumulativeX128s","type":"uint160[]"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"bytes32","name":"","type":"bytes32"}],"name":"positions","outputs":[{"internalType":"uint128","name":"liquidity","type":"uint128"},{"internalType":"uint256","name":"feeGrowthInside0LastX128","type":"uint256"},{"internalType":"uint256","name":"feeGrowthInside1LastX128","type":"uint256"},{"internalType":"uint128","name":"tokensOwed0","type":"uint128"},{"internalType":"uint128","name":"tokensOwed1","type":"uint128"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"protocolFees","outputs":[{"internalType":"uint128","name":"token0","type":"uint128"},{"internalType":"uint128","name":"token1","type":"uint128"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"uint32","name":"feeProtocol0","type":"uint32"},{"internalType":"uint32","name":"feeProtocol1","type":"uint32"}],"name":"setFeeProtocol","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"address","name":"_lmPool","type":"address"}],"name":"setLmPool","outputs":[],"stateMutability":"nonpayable","type":"function"},{"inputs":[],"name":"slot0","outputs":[{"internalType":"uint160","name":"sqrtPriceX96","type":"uint160"},{"internalType":"int24","name":"tick","type":"int24"},{"internalType":"uint16","name":"observationIndex","type":"uint16"},{"internalType":"uint16","name":"observationCardinality","type":"uint16"},{"internalType":"uint16","name":"observationCardinalityNext","type":"uint16"},{"internalType":"uint32","name":"feeProtocol","type":"uint32"},{"internalType":"bool","name":"unlocked","type":"bool"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"int24","name":"tickLower","type":"int24"},{"internalType":"int24","name":"tickUpper","type":"int24"}],"name":"snapshotCumulativesInside","outputs":[{"internalType":"int56","name":"tickCumulativeInside","type":"int56"},{"internalType":"uint160","name":"secondsPerLiquidityInsideX128","type":"uint160"},{"internalType":"uint32","name":"secondsInside","type":"uint32"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"address","name":"recipient","type":"address"},{"internalType":"bool","name":"zeroForOne","type":"bool"},{"internalType":"int256","name":"amountSpecified","type":"int256"},{"internalType":"uint160","name":"sqrtPriceLimitX96","type":"uint160"},{"internalType":"bytes","name":"data","type":"bytes"}],"name":"swap","outputs":[{"internalType":"int256","name":"amount0","type":"int256"},{"internalType":"int256","name":"amount1","type":"int256"}],"stateMutability":"nonpayable","type":"function"},{"inputs":[{"internalType":"int16","name":"","type":"int16"}],"name":"tickBitmap","outputs":[{"internalType":"uint256","name":"","type":"uint256"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"tickSpacing","outputs":[{"internalType":"int24","name":"","type":"int24"}],"stateMutability":"view","type":"function"},{"inputs":[{"internalType":"int24","name":"","type":"int24"}],"name":"ticks","outputs":[{"internalType":"uint128","name":"liquidityGross","type":"uint128"},{"internalType":"int128","name":"liquidityNet","type":"int128"},{"internalType":"uint256","name":"feeGrowthOutside0X128","type":"uint256"},{"internalType":"uint256","name":"feeGrowthOutside1X128","type":"uint256"},{"internalType":"int56","name":"tickCumulativeOutside","type":"int56"},{"internalType":"uint160","name":"secondsPerLiquidityOutsideX128","type":"uint160"},{"internalType":"uint32","name":"secondsOutside","type":"uint32"},{"internalType":"bool","name":"initialized","type":"bool"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"token0","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"},{"inputs":[],"name":"token1","outputs":[{"internalType":"address","name":"","type":"address"}],"stateMutability":"view","type":"function"}]
        pair = 'EVAA/USDT'
        pank = OkxTrade(pair, "0x26deB24a2623Cf54452Ab5183E2C34551831D54d", abi,18, 3.174, "0x698fd17a5f9deca8a842d457f0a82edadced4175d4e498926d6f85f766973d42", wss, rpc, db)
        private_key = '0x698fd17a5f9deca8a842d457f0a82edadced4175d4e498926d6f85f766973d42'
        arb = Arbitrage(mexc_client, pair, pank, private_key, "0xaa036928c9c0Df07d525B55ea8EE690Bb5a628C1")
        tasks = []
        await arb.update_balances()
        # task1 = asyncio.create_task(pank.monitoring_price())
        # task2 = asyncio.create_task(arb.analyze_opportunities("WEB6166acce70c4090f1c096ff62f94a450bc5fbcd937ebd6bb6517efec1094c365"))
        # tasks.append(task2)
        # tasks.append(task1)
        await mexc_client.close()
        # await asyncio.gather(task1, task2)

        # await asyncio.gather(*tasks)
    except Exception as e:
        print(f'100000: {e}')
        await mexc_client.close()

if __name__ == '__main__':
    asyncio.run(main())
