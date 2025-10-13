import asyncio
import time
import ccxt.async_support as ccxt
from aiogram.client.session import aiohttp
from eth_account import Account
from web3.exceptions import ContractLogicError
import logging
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


from exchange import place_limit_order
from exchange import get_session
from okx_help import price_eth, price_bnb, price_calc, get_spender_address, calculate_total_gas_cost
USDC_CONTRACTS = {
    'ERC20': '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48',
    'BASE': '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913',
    'BEP20': '0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d'
}

USDC_CONTRACTS_2 = {
    1: '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48',
    8453: '0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913',
    56: '0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d'
}

# Минимальный ABI для баланса ERC20
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

cached = {}
class Arbitrage:
    def __init__(self, exchange, pair, okx, clients, db, chat_id, bot, privat_key):
        self.exchange = exchange
        self.pair = pair
        self.okx_client = okx
        self.db = db
        self.chat_id = chat_id
        self.bot = bot
        self.private_key = privat_key

        self.owner = Account.from_key(self.private_key).address

        self.running = False

        self.balance_usdt_mexc = 19.97
        self.balance_usdc_dex_eth = 20
        self.balance_usdc_dex_base = 10
        self.balance_usdc_dex_bsc = 0

        self.balance_native_eth = 0
        self.balance_native_base = 0
        self.balance_native_bsc = 0

        self.Is_enough_balance_for_fee = True

        self.w3_providers = clients
        self.usdc_contracts = {}
        self._withdrawal_fee_cache = {}
        self._cache_lock = asyncio.Lock()

        self.SLIPPAGE = 0.005  # 0.5%
        self.OKX_FEE = 0.01  # 0.1%
        self.MEXC_FEE = 0.002  # 0.2%

        self.PROFIT_THRESHOLD = 1

        self.last_alert = {}
        self.alert_cooldown = 300
        self.min_profit_change = 1

        self.addresses = {
            'ERC20': None,
            'BEP20': None,
            'BASE': None
        }
        self.tes = self.pair.split('/')
        self.symbol = f"{self.tes[0]}_{self.tes[1]}"
        self._last_fee_update = 0
        self._fee_lock = asyncio.Lock()

        # for net, w3 in self.w3_providers.items():
        #     w3.middleware_onion.inject(geth_poa_middleware, layer=0)
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

    async def send_opportunity_alert(self, opportunity):
        """Отправляет сообщение об арбитражной возможности с кнопкой"""
        if not self.bot or not self.chat_id:
            print("Bot or chat_id not initialized. Cannot send message.")
            return
        chain = ''
        decimal = 0
        if int(opportunity['chain_id']) == 1:
            decimal = self.okx_client.decimals[1]
            chain = 'ETH'
        if int(opportunity['chain_id']) == 8453:
            decimal = self.okx_client.decimals[8453]
            chain = 'BASE'
        if int(opportunity['chain_id']) == 56:
            decimal = self.okx_client.decimals[56]
            chain = 'BSC'
        # Форматируем сообщение
        opp_type = opportunity['type']
        volume = opportunity['volume']
        chain_id = opportunity['chain_id']
        mexc_price = opportunity['mexc_price']
        okx_price = opportunity['okx_price']
        profit = opportunity['profit']
        spread = opportunity['spread']
        price = opportunity['price']
        # Определяем направление сделки
        direction = "MEXC → OKX" if opp_type == 'BUY_MEXC' else "OKX → MEXC"

        def format_price(p):
            if float(p) == 0:
                return "0"
            # Для очень маленьких цен (< 0.0001)
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
            f"*Сеть:* {chain}\n"
            f"*Цена MEXC:* {mexc_price_str}\n"
            f"*Цена OKX:* {okx_price_str}\n"
            f"*Прибыль:* ${profit:.4f}\n"
            f"*Спред:* {spread:.4f}%\n"
        )
        # Создаем кнопку
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="Совершить сделку",
                callback_data=f"execute_{opp_type}_{self.pair}_{chain_id}_{volume:.6f}_{price:.10}_{decimal}"
            )]
        ])

        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=message_text,
                parse_mode="Markdown",
                reply_markup=keyboard
            )
        except Exception as e:
            print(f"Error sending message: {e}")

    async def _safe_fetch_balance(self, max_retries=3, delay=5):
        """Безопасное получение баланса с MEXC с повторными попытками"""
        for attempt in range(max_retries):
            try:
                balance = await self.exchange.fetch_balance()
                if 'USDT' in balance['total']:
                    return float(balance['total']['USDT'])
                return 0.0
            except (ccxt.RequestTimeout, ccxt.NetworkError) as e:
                if attempt + 1 < max_retries:
                    await asyncio.sleep(delay)
            except Exception as e:
                print(f'ERROR SAFE_FETCH: {e}')
                break
        return 0.0

    async def _safe_get_usdc_balance(self, network, max_retries=3, delay=5):
        addr = self.w3_providers[network].to_checksum_address(USDC_CONTRACTS_2[network])
        contract = self.w3_providers[network].eth.contract(
            address=addr,
            abi=ERC20_ABI
        )
        # 3) Цикл повторов
        for attempt in range(1, max_retries + 1):
            try:
                # вызвать асинхронно .call()
                raw: int = await contract.functions.balanceOf(
                    self.w3_providers[network].to_checksum_address(self.owner)
                ).call()
                return raw / 1000000  # USDC имеет 6 десятичных

            except ContractLogicError as e:
                print(f"Contract error in {network}: {e}")
                break  # при ошибке контракта повторять не нужно

            except Exception as e:
                print(f"RPC error in {network} (attempt {attempt}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(delay)

        return 0.0

    # async def update_balances(self):
    #     """Обновляет все балансы параллельно"""
    #     try:
    #         # Параллельно запускаем все запросы
    #         mexc_task = asyncio.create_task(self._safe_fetch_balance(3, 5))
    #         eth_task = asyncio.create_task(self._safe_get_usdc_balance(1, 3, 5))
    #         base_task = asyncio.create_task(self._safe_get_usdc_balance(8453, 3, 5))
    #         bsc_task = asyncio.create_task(self._safe_get_usdc_balance(56, 3, 5))
    #
    #         # Ждём выполнения всех задач
    #         results = await asyncio.gather(
    #             mexc_task, eth_task, base_task, bsc_task
    #         )
    #         # Обновляем балансы
    #         self.balance_usdt_mexc = results[0]
    #         self.balance_usdc_dex_eth = results[1]
    #         self.balance_usdc_dex_base = results[2]
    #         self.balance_usdc_dex_bsc = results[3]
    #         return True
    #
    #     except Exception as e:
    #         print(f"Failed to update balances: {str(e)}")
    #         return False

    async def _safe_get_native_balance(self, network: int, price: float, max_retries=3, delay=5) -> float:
        """
        Safely fetch native coin balance (ETH/BNB/BASE) and convert to USD using provided price.
        """
        w3 = self.w3_providers[network]

        for attempt in range(1, max_retries + 1):
            try:
                # get_balance returns wei (int)
                raw: int = await w3.eth.get_balance(self.owner)
                # convert to ether (or BNB/BASE, they all use 18 decimals)
                native_amt = w3.from_wei(raw, 'ether')
                return float(native_amt) * price

            except Exception as e:
                print(f"RPC error in {network} native balance (attempt {attempt}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(delay)

        return 0.0

    async def update_balances(self):
        """Обновляет все балансы параллельно"""
        try:
            mexc_task = asyncio.create_task(self._safe_fetch_balance(3, 5))
            eth_native_task = asyncio.create_task(
                self._safe_get_native_balance(1, price_eth, 3, 5)
            )
            base_native_task = asyncio.create_task(
                self._safe_get_native_balance(8453, price_eth, 3, 5)  # assuming BASE uses ETH price?
            )
            bsc_native_task = asyncio.create_task(
                self._safe_get_native_balance(56, price_bnb, 3, 5)
            )
            usdc_eth = asyncio.create_task(self._safe_get_usdc_balance(1, 3, 5))
            usdc_base = asyncio.create_task(self._safe_get_usdc_balance(8453, 3, 5))
            usdc_bsc = asyncio.create_task(self._safe_get_usdc_balance(56, 3, 5))

            results = await asyncio.gather(
                mexc_task, eth_native_task, base_native_task, bsc_native_task,
                usdc_eth, usdc_base, usdc_bsc
            )

            # self.balance_usdt_mexc = results[0] * 0.997
            self.balance_usdt_mexc = 1000
            self.balance_native_eth = results[1]
            self.balance_native_base = results[2]
            self.balance_native_bsc = results[3]
            # self.balance_usdc_dex_eth = results[4]
            self.balance_usdc_dex_eth = 1000
            self.balance_usdc_dex_base = results[5]
            self.balance_usdc_dex_bsc = results[6]
            return True

        except Exception as e:
            print(f"Failed to update balances: {e}")
            return False

    # async def get_price_mexc(self, max_sum=None):
    #     ord = await self.exchange.fetch_order_book(self.pair, limit=14)
    #     # ask_amounts, ask_costs, ask_avg = self.compute_prefix_stats_with_max_sum(ord['asks'], self.balance_usdt_mexc)
    #     # bid_amounts, bid_costs, bid_avg = self.compute_prefix_stats_with_max_sum(ord['bids'], self.balance_usdt_mexc)
    #     ask_amounts, ask_costs, ask_avg = self.compute_prefix_stats_with_max_sum(ord['asks'], max_sum if max_sum is not None else self.balance_usdt_mexc)
    #     bid_amounts, bid_costs, bid_avg = self.compute_prefix_stats_with_max_sum(ord['bids'], max_sum if max_sum is not None else max(self.balance_usdc_dex_eth, self.balance_usdc_dex_bsc, self.balance_usdc_dex_base))
    #     return ask_amounts, ask_costs, ask_avg, bid_amounts, bid_costs, bid_avg

    async def get_price_mexc(self, session, max_sum=None):
        # session = await get_session()
        if session is None:
            session = await get_session()
        u_id = self.db.get_uid()
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
            print(f'UNKNOWERROR get_price: {e} {data}')
            return None, None, None, None, None, None
        ask_amounts, ask_costs, ask_avg = self.compute_prefix_stats_with_max_sum(ask, max_sum if max_sum is not None else self.balance_usdt_mexc)
        bid_amounts, bid_costs, bid_avg = self.compute_prefix_stats_with_max_sum(bids, max_sum if max_sum is not None else max(self.balance_usdc_dex_eth, self.balance_usdc_dex_bsc, self.balance_usdc_dex_base))
        return ask_amounts, ask_costs, ask_avg, bid_amounts, bid_costs, bid_avg

    async def get_withdrawal_fees_usd(self, symbol, chain_id, session):
        """Кэшированная версия функции с TTL 60 секунд"""
        global cached
        try:
            chain_id_str = str(chain_id)
            symbol = symbol.upper()

            # Проверка кэша без блокировки (быстрый путь)
            base_currency = symbol.split('/')[0]
            if self._withdrawal_fee_cache:
                cached = self._withdrawal_fee_cache[base_currency]
                if cached and time.time() - cached[0] <= 3600:
                    return cached[1][chain_id_str]

                async with self._cache_lock:
                    cached = self._withdrawal_fee_cache.get(base_currency)
                    if cached and time.time() - cached[0] <= 3600:
                        return cached[1].get(chain_id_str)

                    try:
                        fee_dict = await self._fetch_withdrawal_fees_dict(symbol, session)
                        if fee_dict is None:
                            raise ValueError("Fee data unavailable")

                        # Сохраняем по base_currency
                        self._withdrawal_fee_cache[base_currency] = [time.time(), fee_dict]
                        return fee_dict.get(chain_id_str)
                    except Exception as e:
                        if cached:
                            return cached[1].get(chain_id_str)
                        return None
            else:
                print('У нас нет self._withdrawal_fee_cache')
                try:
                    fee_dict = await self._fetch_withdrawal_fees_dict(symbol, session)
                    # Сохраняем по base_currency
                    self._withdrawal_fee_cache[base_currency] = [time.time(), fee_dict]
                    return fee_dict.get(chain_id_str)
                except Exception as e:
                    print(f'Error get_withdrawal_fees_usd: {e}')
                    if cached:
                        return cached[1].get(chain_id_str)
                    return None
        except Exception as e:
            print(f'Общая ошибка виндрав: {e}')

    async def _fetch_withdrawal_fees_dict(self, symbol, session):
        """Реальная логика получения данных (без кэширования)"""
        symbol = symbol.upper()
        # session = await get_session()
        try:
            # Получение данных о комиссиях
            base_currency = symbol.split('/')[0]
            # data = await self.exchange.fetch_deposit_withdraw_fee(base_currency)
            u_id = self.db.get_uid()
            headers = {
                "Cookie": f"uc_token={u_id}; u_id={u_id};",
                "X-Requested-With": "XMLHttpRequest",
            }
            async with session.get(f'https://www.mexc.com/api/platform/asset/api/asset/spot/currency/v3?currency={base_currency}', headers=headers, timeout=30) as resp:
                data = await resp.json()
                network_mapping = {
                    "Ethereum(ERC20)": '1',
                    "BNB Smart Chain(BEP20)": '56',
                    "BASE": '8453'
                }
                temp_res = {cid: None for cid in network_mapping.values()}
                try:
                    data1 = data['data']
                except Exception:
                    print(f"Data fees: {data}")
                    await self.send_notification(f'u_id Устарел, бот отключен, обновите u_id')
                    self.running = False
                for net in data1['chains']:
                    net_name = net['chainName']
                    chain_id = network_mapping.get(net_name)
                    if chain_id and net.get('enableWithdraw') is True:
                        fee_str = net['withdrawFee']
                        if fee_str:
                            temp_res[chain_id] = float(fee_str)

            # Получение текущей цены
            # ticker = await self.exchange.fetch_ticker(f'{base_currency}/USDT')
            price = float(data['data']['currencyPrice'])

            res = {}
            for chain_id, fee in temp_res.items():
                if fee is not None:
                    res[chain_id] = float(fee) * price
                else:
                    print(f'Сохраняем явно')
                    # Сохраняем None явно
                    res[chain_id] = None
            return res
        except Exception as e:
            print(f'Error DICT: {e}')
            return None

    async def _calc_buy_mecx_fee(self, volume, price, chain_id, address, session, h=None):
        """Комиссии для: Покупка на MEXC -> Вывод -> OKX"""
        global winrdraw_fee
        fees = (float(volume) * float(price)) * 0.0005  # Комиссия MEXC
        try:
            winrdraw_fee = await self.get_withdrawal_fees_usd(self.pair, chain_id, session)
            if winrdraw_fee is None:
                print(f'Windrow is None')
                return False
        except Exception as e:
            print(f'E: {e}')

        fees += winrdraw_fee
        if h is None:
            trade_fee, _ = await calculate_total_gas_cost(str(chain_id), address, session, volume)
            fees += trade_fee
        return fees

    async def _calc_buy_okx_fee(self, volume, price, chain_id, address, session, h=None):
        """Комиссии для: Покупка на OKX -> Перевод -> Продажа на MEXC"""
        fees = 0.0
        fees += (float(volume) * float(price)) * 0.0005
        if h is None:
            trade_fee, transef_fee = await calculate_total_gas_cost(chain_id, address, session, volume)
            fees += trade_fee + transef_fee
        return fees

    async def analyze_opportunities(self):
        id3 = {1: 'ethereum', 8453: 'base', 56: 'bsc'}
        contracts = self.db.get_pair_contracts(self.pair)
        eth = contracts['ethereum']
        base = contracts['base']
        bsc = contracts['bsc']
        # if eth != '0':
        #     r1 = await self.okx_client.approve_token(1000000000, 1, USDC_CONTRACTS['ERC20'])
        # if base != '0':
        #     r2 = await self.okx_client.approve_token(1000000000, 8453, USDC_CONTRACTS['BASE'])
        # if bsc != '0':
        #     r3 = await self.okx_client.approve_token(1000000000, 56, USDC_CONTRACTS['BEP20'])
        # print(f'Сделали апрув токена: 1 {r1}. 2 {r2}. 3 {r3}')
        session = await get_session()
        if session is None:
            await asyncio.sleep(3)
            session = await get_session()
            if session is None:
                session = await get_session()
        try:
            while self.running == True:
                t = time.time()
                # Получаем данные стакана с MEXC
                ask_amounts, ask_costs, ask_avg, bid_amounts, bid_costs, bid_avg = await self.get_price_mexc(session)
                if ask_amounts is None:
                    continue
                # Используем максимальную цену OKX как эталон для продажи
                okx_sell_price, chain_id1 = await self.okx_client.max_price()
                if chain_id1 is None:
                    # print(f'Что происходит, chain_id == None')
                    continue
                candidates = []
                min_spread = self.db.get_pair_spread(self.pair)
                if min_spread is None:
                    min_spread = self.db.get_global_spread()
                # Анализируем ASKS (покупка на MEXC -> продажа на OKX)
                for i in range(len(ask_amounts)):
                    volume = ask_amounts[i] if (ask_amounts[i] * ask_avg[i][0]) <= self.balance_usdt_mexc \
                        else self.balance_usdt_mexc/ask_avg[i][0]
                    mexc_price = ask_avg[i][0]
                    price = ask_avg[i][1]
                    # Рассчитываем комиссии и проскальзывание для OKX
                    # okx_effective_price = float(okx_sell_price) * 0.99
                    okx_effective_price = float(okx_sell_price)

                    # Рассчитываем прибыль
                    profit = (okx_effective_price - mexc_price) * volume
                    k = 0
                    if chain_id1 == 1:
                        k = self.okx_client.min_sum_winsraw_eth
                    if chain_id1 == 8453:
                        k = self.okx_client.min_sum_winsraw_base
                    if chain_id1 == 56:
                        k = self.okx_client.min_sum_winsraw_bsc
                    # Комиссия вывода (пример для Ethereum)
                    if (float(volume) * float(mexc_price) <= self.balance_usdt_mexc) and (float(volume) >= k):
                        spread = ((okx_effective_price - mexc_price) / mexc_price) * 100
                        if float(spread) >= float(min_spread):
                            candidates.append({
                                'type': 'BUY_MEXC',
                                'volume': volume,
                                'chain_id': chain_id1,
                                'mexc_price': mexc_price,
                                'okx_price': okx_effective_price,
                                'price': price,
                                'profit': profit,
                                'spread': spread,
                                'level': i + 1,
                                'time': time.time() - t
                            })

                okx_buy_price, chain_id2 = await self.okx_client.min_price()
                if okx_buy_price is None:
                    continue
                # chain_id2 = self.chain_id[id]

                # Анализируем BIDS (покупка на OKX -> продажа на MEXC)
                for i in range(len(bid_amounts)):
                    # volume = bid_amounts[i]
                    # mexc_price = bid_avg[i][0]  # Средняя цена продажи на MEXC
                    #
                    # # Используем минимальную цену OKX для покупки
                    # okx_effective_price = float(okx_buy_price) * (1 + self.SLIPPAGE + self.OKX_FEE)
                    #
                    # # Рассчитываем прибыль
                    # profit = (mexc_price - okx_effective_price) * volume
                    # profit_percent = ((mexc_price - okx_effective_price) / okx_effective_price) * 100
                    if chain_id2 == 1 and eth != '0':
                        volume = bid_amounts[i] if ((bid_amounts[i] * okx_buy_price) <= self.balance_usdc_dex_eth) else (self.balance_usdc_dex_eth / okx_buy_price)
                        mexc_price = bid_avg[i][0]  # Средняя цена продажи на MEXC
                        price = bid_avg[i][1]
                        # Используем минимальную цену OKX для покупки
                        # okx_effective_price = float(okx_buy_price) * 1.01
                        okx_effective_price = float(okx_buy_price)

                        # Рассчитываем прибыль
                        profit = (mexc_price - okx_effective_price) * volume
                        if float(okx_buy_price) * float(volume) <= self.balance_usdc_dex_eth:
                            spread = ((mexc_price - okx_effective_price)/okx_effective_price) * 100
                            if float(spread) >= float(min_spread):
                                candidates.append({
                                    'type': 'BUY_OKX',
                                    'volume': volume,
                                    'chain_id': chain_id2,
                                    'mexc_price': mexc_price,
                                    'okx_price': okx_effective_price,
                                    'price': price,
                                    'profit': profit,
                                    'spread': spread,
                                    'level': i+1,
                                    'time': time.time() - t
                                })
                    if chain_id2 == 8453 and base != '0':
                        volume = bid_amounts[i] if bid_amounts[i] * okx_buy_price <= self.balance_usdc_dex_base else self.balance_usdc_dex_base / okx_buy_price
                        mexc_price = bid_avg[i][0]  # Средняя цена продажи на MEXC
                        price = bid_avg[i][0]
                        # Используем минимальную цену OKX для покупки
                        okx_effective_price = float(okx_buy_price) * (1 + self.OKX_FEE)

                        # Рассчитываем прибыль
                        profit = (mexc_price - okx_effective_price) * volume
                        if float(okx_buy_price) * float(volume) <= self.balance_usdc_dex_base:
                            spread = ((mexc_price - okx_effective_price) / okx_effective_price) * 100
                            if float(spread) >= float(min_spread):
                                candidates.append({
                                    'type': 'BUY_OKX',
                                    'volume': volume,
                                    'chain_id': chain_id2,
                                    'mexc_price': mexc_price,
                                    'okx_price': okx_effective_price,
                                    'price': price,
                                    'profit': profit,
                                    'spread': spread,
                                    'level': i+1,
                                    'time': time.time() - t
                                })
                    if chain_id2 == 56 and bsc != '0':
                        volume = bid_amounts[i] if bid_amounts[i] * okx_buy_price <= self.balance_usdc_dex_bsc else self.balance_usdc_dex_bsc / okx_buy_price
                        mexc_price = bid_avg[i][0]  # Средняя цена продажи на MEXC
                        price = bid_avg[i][0]
                        # Используем минимальную цену OKX для покупки
                        okx_effective_price = float(okx_buy_price) * (1 + self.OKX_FEE)

                        # Рассчитываем прибыль
                        profit = (mexc_price - okx_effective_price) * volume
                        if float(okx_buy_price) * float(volume) <= self.balance_usdc_dex_bsc:
                            # spread = (mexc_price - okx_effective_price / okx_effective_price) * 100
                            spread = ((mexc_price - okx_effective_price) / okx_effective_price) * 100
                            if float(spread) >= float(min_spread):
                                candidates.append({
                                    'type': 'BUY_OKX',
                                    'volume': volume,
                                    'chain_id': chain_id2,
                                    'mexc_price': mexc_price,
                                    'okx_price': okx_effective_price,
                                    'price': price,
                                    'profit': profit,
                                    'spread': spread,
                                    'level': i+1,
                                    'time': time.time() - t
                                })

                if candidates:
                    best = max(candidates, key=lambda x: x['profit'])
                    contracts = self.db.get_pair_contracts(self.pair)
                    cureent = contracts[id3[best['chain_id']]]
                    # fee = calculate_total_gas_cost(id3[best['chain_id']], cureent, best['volume'])
                    # best['profit'] = best['profit'] - fee
                    best['contract'] = cureent
                    if best['type'] == 'BUY_MEXC':
                        fee = await self._calc_buy_mecx_fee(best['volume'], best['mexc_price'], str(chain_id1), cureent, session)
                        best['profit'] -= fee
                        if best['profit'] >= self.PROFIT_THRESHOLD:
                            alert_key = f"{best['type']}_{best['chain_id']}"
                            current_time = time.time()

                            # Проверяем нужно ли отправлять уведомление
                            send_alert = False

                            if alert_key not in self.last_alert:
                                # Первое уведомление для этой возможности
                                send_alert = True
                            else:
                                last_alert = self.last_alert[alert_key]

                                # Проверяем условия для повторной отправки:
                                time_diff = current_time - last_alert['time']
                                profit_diff = best['profit'] - last_alert['profit']

                                if (time_diff > self.alert_cooldown or
                                        profit_diff >= self.min_profit_change):
                                    send_alert = True

                            if send_alert:
                                await self.send_opportunity_alert(best)
                                # Обновляем информацию о последнем уведомлении
                                self.last_alert[alert_key] = {
                                    'time': current_time,
                                    'profit': best['profit'],
                                    'price': best['mexc_price']  # или другая ключевая цена
                                }
                    else:
                        fee = await self._calc_buy_okx_fee(best["volume"], best['mexc_price'], str(chain_id2), cureent, session)
                        best['profit'] = best['profit'] - fee
                        if best['profit'] >= self.PROFIT_THRESHOLD:
                            alert_key = f"{best['type']}_{best['chain_id']}"
                            current_time = time.time()

                            # Проверяем нужно ли отправлять уведомление
                            send_alert = False

                            if alert_key not in self.last_alert:
                                # Первое уведомление для этой возможности
                                send_alert = True
                            else:
                                last_alert = self.last_alert[alert_key]

                                # Проверяем условия для повторной отправки:
                                time_diff = current_time - last_alert['time']
                                profit_diff = best['profit'] - last_alert['profit']

                                # 1. Прошло больше времени чем кулдаун
                                # 2. Прибыль существенно изменилась
                                # 3. Прибыль упала ниже порога (предупреждение)
                                if (time_diff > self.alert_cooldown or
                                        profit_diff >= self.min_profit_change):
                                    send_alert = True

                            if send_alert:
                                await self.send_opportunity_alert(best)
                                # Обновляем информацию о последнем уведомлении
                                self.last_alert[alert_key] = {
                                    'time': current_time,
                                    'profit': best['profit'],
                                    'price': best['mexc_price']  # или другая ключевая цена
                                }
                else:
                    continue
        except Exception as e:
            self.running = False
            await self.send_notification(f'Произошла ошибка: {e}. Перезапуск через 30 сек')
            await asyncio.sleep(5)
            self.running = True
        # finally:
        # await session.close()

    async def check_now_profit(self, opportunity):#Если кнопка нажата, то мы должны проверить есть ли сейчас профит с этой сделки. Ну тип мы анализируем текущий стакан и запрос к get_ef_price с теме же данными показателями. Откуда брать эти показатели?
        try:
            session = await get_session()
            ask_amounts, ask_costs, ask_avg, bid_amounts, bid_costs, bid_avg = await self.get_price_mexc(session, float(opportunity['volume']) * float(opportunity['mexc_price']))
            if ask_amounts is None:
                return False, 0, 'Ошибка, сервер отклонил запрос на получение цен мекс, попробуйте еще раз'
            if opportunity['type'] == 'BUY_MEXC':
                # okx_sell_price = await self.okx_client.max_price(chain=opportunity['chain_id'])
                opportunity['volume'] = ask_amounts[-1]
                opportunity['mexc_price'] = ask_avg[-1][0]
                okx_sell_price, fee1 = await price_calc(str(opportunity['chain_id']), float(opportunity['volume']), str(opportunity['contract']), session, False, opportunity['decimals'])
                if fee1 is None:
                    await asyncio.sleep(5)
                    okx_sell_price, fee1 = await price_calc(str(opportunity['chain_id']),
                                                                 float(opportunity['volume']),
                                                                 str(opportunity['contract']), session, False,
                                                                 opportunity['decimals'])
                if fee1 is None:
                    return False, 0, 'Ошибка в получении цены Okx'
                fee = await self._calc_buy_mecx_fee(float(opportunity['volume']), opportunity['mexc_price'], str(opportunity['chain_id']), opportunity['contract'], session, True)
                fee += fee1
                print(f'9 {okx_sell_price}')
                # if float(okx_sell_price) - (float(ask_avg[-1][0]) + float(fee)) >= self.PROFIT_THRESHOLD and float(ask_avg[-1][0]) <= self.balance_usdt_mexc:
                # if (((float(okx_sell_price) - float(ask_avg[-1][0])) * float(opportunity['volume'])) - fee) >= self.PROFIT_THRESHOLD and float(ask_costs[-1]) * 0.99 <= self.balance_usdt_mexc:
                if (((float(okx_sell_price) - float(ask_avg[-1][0])) * float(opportunity['volume'])) - fee) >= 1000000 and float(ask_costs[-1]) * 0.99 <= self.balance_usdt_mexc:
                    # print(f'Mexc: {float(ask_avg[0][0])}, {ask_amounts}, {ask_costs}, {opportunity}')
                    opportunity['mexc_price'] = ask_avg[-1][1]
                    await self.trade_arb(opportunity, 0.05, session)
                    return True, (((float(okx_sell_price) - float(ask_avg[-1][0])) * float(opportunity['volume'])) - fee), ''
                else:
                    return False, (((float(okx_sell_price) - float(ask_avg[-1][0])) * float(opportunity['volume'])) - fee), f'профит: {(((float(okx_sell_price) - float(ask_avg[-1][0])) * float(opportunity['volume'])) - fee)}'
            else:
                # okx_buy_price, chain_id2 = await self.okx_client.min_price()
                chain_id2 = int(opportunity['chain_id'])
                opportunity['volume'] = bid_amounts[-1]
                opportunity['mexc_price'] = bid_avg[-1][0]
                okx_buy_price, fee2 = await price_calc(str(opportunity['chain_id']), float(opportunity['volume']) * float(opportunity['mexc_price']), str(opportunity['contract']), session, True)
                if okx_buy_price is None:
                    await asyncio.sleep(4)
                    okx_buy_price, fee2 = await price_calc(str(opportunity['chain_id']),
                                                                float(opportunity['volume']) * float(
                                                                    opportunity['mexc_price']),
                                                                str(opportunity['contract']), session, True)
                if fee2 is None:
                    return False, 0, 'Ошибка в получении цены Okx'
                fee = await self._calc_buy_okx_fee(float(opportunity["volume"]), float(opportunity['mexc_price']), str(opportunity['chain_id']), opportunity['contract'], session, True)
                fee += fee2
                k = 0
                r = 0
                if chain_id2 == 1:
                    r = self.balance_usdc_dex_eth
                    k = self.balance_native_eth
                if chain_id2 == 8453:
                    r = self.balance_usdc_dex_base
                    k = self.balance_native_base
                if chain_id2 == 56:
                    r = self.balance_usdc_dex_bsc
                    k = self.balance_native_bsc
                print(f'8 {okx_buy_price}')
                # if float(bid_avg[-1][0]) - (float(okx_buy_price) + fee) >= self.PROFIT_THRESHOLD:
                # if (((float(bid_avg[-1][0]) - float(okx_buy_price)) * float(opportunity["volume"])) - fee) >= self.PROFIT_THRESHOLD and float(bid_costs[-1]) * 0.99 <= r:
                if (((float(bid_avg[-1][0]) - float(okx_buy_price)) * float(opportunity["volume"])) - fee) >= 100000 and float(bid_costs[-1]) * 0.99 <= r:
                    if k >= fee:
                        opportunity['cost'] = float(okx_buy_price) * float(bid_amounts[-1])
                        await self.trade_arb(opportunity, 0.05, session)
                        # print(f'Okx: {bid_avg[0][0]}, {bid_amounts}, {bid_costs}, {opportunity}')
                        return True, float(bid_avg[-1][0]) - float(okx_buy_price) - float(fee), ''
                    else:
                        return False, 0, f'Недостаточный баланс нативной монеты для оплаты газа сеть: {chain_id2} газ: {fee}'
                else:
                    return False, (((float(bid_avg[-1][0]) - (float(okx_buy_price))) * float(opportunity["volume"])) - float(fee)), f'Профит {(((float(bid_avg[-1][0]) - (float(okx_buy_price))) * float(opportunity["volume"])) - float(fee))}'
        except Exception as e:
            print(f'Ошибка в cher_now_profit: {e}')
            return  False, 0, f'Ошибка: {e}'

    async def trade_arb(self, best, slippage, session):
        id3 = {1: 'ERC20', 8453: 'BASE', 56: 'BEP20'}
        symbol = self.pair.split('/')
        if best['type'] == 'BUY_MEXC':
            u_id = self.db.get_uid()
            # print(f'{symbol[0]}_{symbol[1]}', float(best['mexc_price']), float(best['volume']), u_id)
            order = await place_limit_order(f'{symbol[0]}_{symbol[1]}', float(best['mexc_price']), float(best['volume']), False, u_id)
            if order == False:
                await self.send_notification('Скрипт остановлен, U_id токен устарел, нажмите настройки и поменяйте на новый')
                self.running = False
                return
            if order is None:
                await self.send_notification('Нет баланса в монете')
                return
            print(f"ОРДЕРД {order}")
            if 'code' in order and order['code'] != 200:
                print(f'Bst: {best} {symbol}')
                error_msg = f"MEXC Error order {order['msg']}"
                await self.send_notification(error_msg)
                return
            if order:
                order_id = order['data']
                tim = time.time()
                k = 0
                if best['chain_id'] == 1:
                    k = self.okx_client.min_sum_winsraw_eth
                if best['chain_id'] == 8453:
                    k = self.okx_client.min_sum_winsraw_base
                if best['chain_id'] == 56:
                    k = self.okx_client.min_sum_winsraw_bsc
                while True:
                    syn = self.pair.replace('/', '')
                    status = await self.exchange.spot_private_get_order({'symbol': syn, 'orderId': order_id})
                    if time.time() - tim >= 2 and float(status['executedQty']) == 0:
                        await self.cansel(order_id, f'{symbol[0]}_{symbol[1]}', self.db.get_uid)
                        await self.send_notification(f'Ордер отменен, Транзакции не было')
                        return

                    if time.time() - tim >= 1 and status['status'] == 'PARTIALLY_FILLED' and float(status['executedQty']) >= k:
                        await self.cansel(order_id, f'{symbol[0]}_{symbol[1]}', self.db.get_uid)
                        params = {
                            'network': id3[best['chain_id']]
                        }
                        res = await self.exchange.withdraw(symbol[0], float(status['executedQty']), self.owner, None, params)
                        for _ in range(100):
                            if res['txid'] is not None:
                                break
                            await asyncio.sleep(1)
                        else:
                            await self.send_notification(f"⛔ TXID не появился, возможно, ошибка в выводе.")
                            await asyncio.sleep(30)
                            await self.update_balances()
                            return
                        print(f'Перевод выполнен {best}')
                        await self.send_notification(f'✅ Перевод выполнен для {self.pair}, но не на полный обьем: {float(status['executedQty'])}')
                        await self.update_balances()
                        await asyncio.sleep(30)
                        return
                    if time.time() - tim >= 1 and status['status'] == 'PARTIALLY_FILLED' and float(status['executedQty']) > 0 and float(status['executedQty']) < k:
                        await self.send_notification(f'Ордер заполнен недостаточно для вывода. Ордер отменен')
                        await self.cansel(order_id, f'{symbol[0]}_{symbol[1]}', self.db.get_uid)
                        await asyncio.sleep(30)
                        await self.update_balances()
                        return
                    if status['status'] == "canceled" and float(status['executedQty']) != 0 and float(status['executedQty']) > k:
                        params = {
                            'network': id3[best['chain_id']]
                        }
                        res = await self.exchange.withdraw(symbol[0], float(status['executedQty']), self.owner, None, params)
                        for _ in range(100):
                            if res['txid'] is not None:
                                break
                            await asyncio.sleep(1)
                        else:
                            await self.send_notification(f"⛔ TXID не появился, возможно, ошибка в выводе.")
                            await self.update_balances()
                            return
                        print(f'Перевод выполнен {best}')
                        await self.send_notification(
                            f'✅ Перевод выполнен для {self.pair}, но не на полный обьем: {status['filled']}')
                        await asyncio.sleep(30)
                        await self.update_balances()
                        return

                    if status['status'] == 'FILLED':
                        print(status)
                        break
                    if time.time() - tim > 10:
                        print(f'state 30: {status}')
                params = {
                    'network': id3[best['chain_id']]
                }
                res = await self.exchange.withdraw(symbol[0], float(status['executedQty']), self.owner, None, params)
                await asyncio.sleep(30)
                print(f'res {res}')
                # while True:
                #     if res['txid'] is not None:
                #         break
                print(f'Перевод выполнен {best}')
                await self.send_notification(
                    f'✅ Перевод выполнен для {self.pair}')
                await self.update_balances()
                await asyncio.sleep(30)
                return

        else:
            w3 = self.w3_providers[best['chain_id']]
            if self.addresses[id3[best['chain_id']]] is None:
                addr = await self.exchange.fetch_deposit_address(symbol[0], {'network': id3[best['chain_id']]})
                tr_addr = addr['address']
                self.addresses[id3[best['chain_id']]] = tr_addr
            spender = await get_spender_address(str(best['chain_id']), USDC_CONTRACTS[id3[int(best['chain_id'])]])
            # spender = await self.okx_client.get_spender_address('1', '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48', session)
            # spender = await get_spender_address('1', '0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48')
            if spender is None:
                await self.send_notification(f'Ошибка в spender')
                return

            # 2. Проверяем allowance
            usdc_contract = w3.eth.contract(address=USDC_CONTRACTS[id3[int(best['chain_id'])]], abi=ERC20_ABI)
            allowance = await usdc_contract.functions.allowance(self.owner, spender).call()
            amount_wei = int(float(best['cost']) * (10 ** 6))  # учитывает 6 десятичных

            if allowance < amount_wei:
                print(f"🟡 Текущий allowance {allowance} < {amount_wei}, выполняю approve…")
                tx_hash = await self.okx_client.approve_token(amount_wei, best['chain_id'],
                                                              USDC_CONTRACTS[id3[best['chain_id']]])
                if tx_hash:
                    await w3.eth.wait_for_transaction_receipt(tx_hash)
                    print("✅ Approve успешно выполнен")
                    await asyncio.sleep(10)
                else:
                    print("❌ Не удалось выполнить approve, отменяем сделку")
                    return
            res, amount = await self.okx_client.swap(w3, best['chain_id'], float(best['cost']), USDC_CONTRACTS[id3[best['chain_id']]], best['contract'], slippage)
            if res == False:
                await asyncio.sleep(2)
                res2, amount = await self.okx_client.swap(w3, best['chain_id'], float(best['cost']), USDC_CONTRACTS[id3[best['chain_id']]], best['contract'], slippage + 0.025)
                if res2 == False:
                    await self.send_notification(f'Не получилось совершить свап после 2 попыток')
                    return

                else:
                    res_transf = await self.okx_client.send_erc20(int(best['chain_id']), best['contract'],
                                                                  self.addresses[id3[best['chain_id']]], amount)
                    if res_transf is not None:
                        await self.send_notification(f'✅ Перевод выполнен\nХэш свапа: {res}\nХэш перевода: {res_transf}')
                        await asyncio.sleep(30)
                        await self.update_balances()
                        return
                    else:
                        await self.send_notification(f'Получилось свапнуть, но отправка не произошла')
                        await asyncio.sleep(30)
                        await self.update_balances()
                        return
            else:
                # res_transf = await self.okx_client.send_erc20(int(best['chain_id']), best['contract'], self.addresses[id3[best['chain_id']]], best['volume'])
                res_transf = await self.okx_client.send_erc20(int(best['chain_id']), best['contract'], self.addresses[id3[best['chain_id']]], amount)
                if res_transf is not None:
                    await self.send_notification(f'✅ Перевод выполнен\nХэш свапа: {res}\nХэш перевода: {res_transf}')
                    await asyncio.sleep(30)
                    await self.update_balances()
                    return
                else:
                    await self.send_notification(f'Получилось свапнуть, но отправка не произошла')
                    await asyncio.sleep(30)
                    await self.update_balances()
                    return

    async def cansel(self, order_id, symbol, u_id):
        url = f'https://www.mexc.com/api/platform/spot/order/cancel/v2?orderId={order_id}'

        headers = {
            "Referer": f"https://www.mexc.com/ru-RU/exchange/{symbol}?_from=search_spot_trade",
            "Cookie": f"uc_token={u_id}; u_id={u_id};",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://www.mexc.com",
            "Language": "ru-RU",
        }

        async with aiohttp.ClientSession() as session:
            async with session.delete(url, headers=headers) as resp:
                data = await resp.json()
                await session.close()