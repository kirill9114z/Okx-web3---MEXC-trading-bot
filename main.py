import sys
import asyncio
from trade import Arbitrage
from web3 import AsyncWeb3
from web3.exceptions import ContractLogicError
from database import Database
import logging
import ccxt.async_support as ccxt
from web3.middleware import ExtraDataToPOAMiddleware
from config import API_KEY_MEXC, API_SECRET_MEXC, RPC_ETH, RPC_BSC, RPC_BASE

from okx_trade import OkxTrade
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("ArbitrageBot")


class MainArb:
    def __init__(self):
        pass

    async def initialize(self):
        pass

    async def start(self):
        pass

    async def stop(self):
        pass

    async def restrart(self):
        await self.stop()
        await asyncio.sleep(30)
        await self.start()

active_arbitrage_instances = {}
async def monitor_pair(pair_name, db, mexc, clients, chat_id, bot, privat_key):
    """Запускает мониторинг для конкретной пары"""
    logger.info(f"Starting monitoring for {pair_name}")

    # Получаем контракты для пары
    contracts = db.get_pair_contracts(pair_name)
    if not contracts:
        logger.error(f"No contracts found for {pair_name}")
        return

    okx_client = OkxTrade(pair_name, db, mexc, clients, privat_key)
    arbitrage = Arbitrage(mexc, pair_name, okx_client, clients, db, chat_id, bot, privat_key)
    active_arbitrage_instances[pair_name] = arbitrage
    arbitrage.running = True
    okx_client.run = True
    await okx_client.initialize()
    # Создаем экземпляр арбитражного бота для пары
    await asyncio.sleep(10)
    await arbitrage.update_balances()
    try:
        logger.info(f"We start analyzing to {pair_name}")
        await arbitrage.analyze_opportunities()
    except Exception as e:
        logger.exception(f"Error monitoring {pair_name}: {str(e)}")
    finally:
        logger.info(f"Monitoring stopped for {pair_name}")
        okx_client.run = False
        if pair_name in active_arbitrage_instances:
            del active_arbitrage_instances[pair_name]


async def main(chat_id, bot):
    try:
        db = Database()
        prvat = db.get_private_key()
        pairs = db.get_all_pairs()
        if not pairs:
            logger.warning("No pairs found in database. Add pairs via Telegram bot.")
            return

        mexc_client = ccxt.mexc({
            'apiKey': API_KEY_MEXC,
            'secret': API_SECRET_MEXC,  # НУЖНО БУДЕТ ИМПОРТИРОВАТЬ ИЗ КОНФИГА АПИ_КЕЙ И АПИСИКРЕТ
            'enableRateLimit': True,
            'timeout': 30000
        })
        w3_providers = {
            1: AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(RPC_ETH)),
            8453: AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(RPC_BASE)),
            56: AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(RPC_BSC))
        }
        for w3 in w3_providers.values():
            w3.middleware_onion.inject(ExtraDataToPOAMiddleware, name='ExtraDataToPOA', layer=0)
        # Создаем задачи для мониторинга каждой пары
        tasks = []
        for pair in pairs:
            task = asyncio.create_task(monitor_pair(pair, db, mexc_client, w3_providers, chat_id, bot, prvat))
            tasks.append(task)
        # Запускаем все задачи параллельно
        await asyncio.gather(*tasks)
    except Exception as e:
        logger.exception(f"Critical error in main: {e}")

if __name__ == "__main__":
    asyncio.run(main())
