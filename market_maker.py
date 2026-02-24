#!/usr/bin/env python3
"""
Predict.fun Passive Points Market Maker Bot
============================================

Стратегия PASSIVE_POINTS:
  - Ордера выставляются далеко от текущей цены (спред 15-20%)
  - Цель — генерировать Predict Points (2x multiplier на uncertain markets)
  - Ордера висят в стакане, но не исполняются при нормальном движении цены

Защита:
  - Автоматическая отмена ордеров при приближении цены (Price Protection)
  - Фильтрация рынков по ликвидности и вероятности
  - Запрет новых ордеров при наличии открытой позиции

DISCLAIMER: Это не финансовая рекомендация. Тестируйте на малых суммах.
Predict.fun может изменить правила начисления поинтов в любой момент.
"""

import asyncio
import os
import sys
import signal
import logging
import time
import uuid
from decimal import Decimal, ROUND_DOWN
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import aiohttp
from dotenv import load_dotenv

from predict_sdk import (
    OrderBuilder,
    ChainId,
    Side,
    LimitHelperInput,
    BuildOrderInput,
)

WEI = 10**18
USDT_DECIMALS = 18

API_BASE = "https://api.predict.fun"

logger = logging.getLogger("PredictBot")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class BotConfig:
    chain_id: str = "BNB_MAINNET"

    # Фильтрация рынков
    min_liquidity_usd: float = 1000.0
    max_liquidity_usd: float = 25000.0
    min_probability: float = 0.45
    max_probability: float = 0.55
    max_active_markets: int = 3

    # Позиции
    max_position_usd: float = 100.0
    order_size_usd: float = 20.0

    # Стратегия PASSIVE_POINTS
    strategy_mode: str = "PASSIVE_POINTS"
    passive_spread: float = 0.15  # 15% от mid-price

    # Price Protection
    cancel_when_price_close: bool = True
    price_proximity_threshold: float = 0.03  # 3%
    price_check_interval_sec: int = 15

    # Ребалансировка
    rebalance_interval_sec: int = 300
    stop_new_orders_after_fill: bool = True

    log_level: str = "INFO"


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class MarketInfo:
    id: int
    title: str
    question: str
    fee_rate_bps: int
    is_neg_risk: bool
    is_yield_bearing: bool
    outcomes: list = field(default_factory=list)
    chance: float = 0.50            # YES probability 0..1
    liquidity_usd: float = 0.0
    token_ids: dict = field(default_factory=dict)  # {0: yes_token_id, 1: no_token_id}


@dataclass
class ActiveOrder:
    order_hash: str
    market_id: int
    outcome_index: int  # 0=YES, 1=NO
    price: Decimal
    size_usd: float
    placed_at: datetime
    token_id: str


# ---------------------------------------------------------------------------
# API Client
# ---------------------------------------------------------------------------

class PredictAPIClient:
    """Тонкая обёртка над Predict REST API."""

    def __init__(self, api_key: str | None = None):
        self._api_key = api_key
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self):
        if self._session is None or self._session.closed:
            headers = {"Content-Type": "application/json"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            self._session = aiohttp.ClientSession(
                base_url=API_BASE,
                headers=headers,
            )

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_markets(self, status: str = "OPEN", first: int = 50) -> list[dict]:
        await self._ensure_session()
        all_markets = []
        cursor = None

        while True:
            params = {"status": status, "first": str(first)}
            if cursor:
                params["after"] = cursor

            async with self._session.get("/v1/markets", params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()

            all_markets.extend(data.get("data", []))
            cursor = data.get("cursor")
            if not cursor or len(data.get("data", [])) < first:
                break

        return all_markets

    async def get_market(self, market_id: int) -> dict | None:
        await self._ensure_session()
        async with self._session.get(f"/v1/markets/{market_id}") as resp:
            if resp.status == 404:
                return None
            resp.raise_for_status()
            data = await resp.json()
            return data.get("data")

    async def get_market_stats(self, market_id: int) -> dict | None:
        await self._ensure_session()
        async with self._session.get(f"/v1/markets/{market_id}/stats") as resp:
            if resp.status == 404:
                return None
            resp.raise_for_status()
            data = await resp.json()
            return data.get("data")

    async def get_orderbook(self, market_id: int) -> dict | None:
        await self._ensure_session()
        async with self._session.get(f"/v1/markets/{market_id}/orderbook") as resp:
            if resp.status == 404:
                return None
            resp.raise_for_status()
            data = await resp.json()
            return data.get("data")

    async def get_open_orders(self, status: str = "OPEN") -> list[dict]:
        await self._ensure_session()
        orders = []
        cursor = None

        while True:
            params = {"status": status, "first": "100"}
            if cursor:
                params["after"] = cursor

            async with self._session.get("/v1/orders", params=params) as resp:
                resp.raise_for_status()
                data = await resp.json()

            orders.extend(data.get("data", []))
            cursor = data.get("cursor")
            if not cursor or len(data.get("data", [])) < 100:
                break

        return orders

    async def submit_order(self, order_payload: dict) -> dict:
        await self._ensure_session()
        async with self._session.post("/v1/orders", json=order_payload) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def remove_orders(self, order_ids: list[str]) -> dict:
        """Быстрое удаление ордеров из orderbook (off-chain)."""
        await self._ensure_session()
        async with self._session.post(
            "/v1/orders/remove",
            json={"orderIds": order_ids},
        ) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def get_positions(self, address: str) -> list[dict]:
        await self._ensure_session()
        async with self._session.get(f"/v1/positions/{address}") as resp:
            if resp.status == 404:
                return []
            resp.raise_for_status()
            data = await resp.json()
            return data.get("data", [])


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class MarketMakerBot:
    def __init__(
        self,
        config: BotConfig,
        private_key: str,
        predict_account: str | None = None,
        api_key: str | None = None,
    ):
        self.config = config
        self.private_key = private_key
        self.predict_account = predict_account

        self.api = PredictAPIClient(api_key=api_key)

        chain = ChainId.BNB_MAINNET if config.chain_id == "BNB_MAINNET" else ChainId.BNB_TESTNET
        self.builder = OrderBuilder.make(chain, private_key)

        self.active_orders: dict[str, ActiveOrder] = {}
        self.filled_markets: set[int] = set()
        self._running = False

    # ------------------------------------------------------------------
    # Market filtering
    # ------------------------------------------------------------------

    async def get_suitable_markets(self) -> list[MarketInfo]:
        """
        Ищем рынки с вероятностью 45-55% (максимальные поинты, 2x multiplier)
        и достаточной ликвидностью.
        """
        raw_markets = await self.api.get_markets(status="OPEN")
        suitable: list[MarketInfo] = []

        for m in raw_markets:
            if m.get("tradingStatus") != "OPEN":
                continue

            market_id = m["id"]
            fee_rate_bps = m.get("feeRateBps", 0)

            outcomes = m.get("outcomes", [])
            token_ids = {}
            for i, outcome in enumerate(outcomes):
                tid = outcome.get("onChainId") or outcome.get("tokenId")
                if tid:
                    token_ids[i] = str(tid)

            chance = self._extract_chance(m)
            if chance is None:
                continue

            if not (self.config.min_probability <= chance <= self.config.max_probability):
                continue

            stats = await self.api.get_market_stats(market_id)
            liquidity = 0.0
            if stats:
                liquidity = float(stats.get("totalLiquidityUsd", 0))

            if not (self.config.min_liquidity_usd <= liquidity <= self.config.max_liquidity_usd):
                continue

            info = MarketInfo(
                id=market_id,
                title=m.get("title", ""),
                question=m.get("question", ""),
                fee_rate_bps=fee_rate_bps,
                is_neg_risk=m.get("isNegRisk", False),
                is_yield_bearing=m.get("isYieldBearing", False),
                outcomes=outcomes,
                chance=chance,
                liquidity_usd=liquidity,
                token_ids=token_ids,
            )
            suitable.append(info)

            if len(suitable) >= self.config.max_active_markets * 2:
                break

        suitable.sort(key=lambda x: abs(x.chance - 0.50))
        logger.info(f"Найдено {len(suitable)} подходящих рынков (prob {self.config.min_probability}-{self.config.max_probability})")
        return suitable

    @staticmethod
    def _extract_chance(market_data: dict) -> float | None:
        """Извлекаем YES-вероятность из данных рынка."""
        if "outcomePrices" in market_data:
            prices = market_data["outcomePrices"]
            if isinstance(prices, list) and len(prices) > 0:
                return float(prices[0])

        outcomes = market_data.get("outcomes", [])
        for o in outcomes:
            if o.get("name", "").lower() == "yes" and "price" in o:
                return float(o["price"])
            if "chance" in o:
                return float(o["chance"])

        if "chance" in market_data:
            val = float(market_data["chance"])
            return val / 100.0 if val > 1 else val

        return None

    # ------------------------------------------------------------------
    # Smart price calculation (PASSIVE_POINTS)
    # ------------------------------------------------------------------

    async def calculate_passive_prices(self, market: MarketInfo) -> tuple[Decimal, Decimal]:
        """
        Рассчитываем цены для PASSIVE_POINTS:
        - Далеко от mid-price, чтобы не исполнялись
        - Но внутри стакана, чтобы начислялись поинты

        YES bid = mid - spread (покупаем YES дёшево)
        NO  bid = (1 - mid) - spread (покупаем NO дёшево)
        """
        mid = Decimal(str(market.chance))
        spread = Decimal(str(self.config.passive_spread))

        orderbook = await self.api.get_orderbook(market.id)

        bid_yes = mid - spread
        bid_no = (Decimal("1") - mid) - spread

        if orderbook:
            bid_yes = self._adjust_price_to_book(bid_yes, orderbook.get("bids", []), side="bid")
            bid_no = self._adjust_price_to_book(bid_no, orderbook.get("bids", []), side="bid")

        bid_yes = self._clamp_price(bid_yes)
        bid_no = self._clamp_price(bid_no)

        logger.info(
            f"  Цены для '{market.title[:40]}': "
            f"mid={mid:.3f}, YES_bid={bid_yes:.3f}, NO_bid={bid_no:.3f}"
        )
        return bid_yes, bid_no

    @staticmethod
    def _adjust_price_to_book(
        target_price: Decimal,
        book_levels: list,
        side: str = "bid",
    ) -> Decimal:
        """
        Умный расчёт: ставим ордер не на самый край стакана,
        а чуть глубже, чтобы не зацепили крупные свипы.
        """
        if not book_levels:
            return target_price

        prices = sorted([Decimal(str(lvl[0])) for lvl in book_levels])

        if side == "bid":
            nearby = [p for p in prices if abs(p - target_price) < Decimal("0.05")]
            if nearby:
                best_nearby = min(nearby)
                safer_price = best_nearby - Decimal("0.01")
                return safer_price if safer_price > Decimal("0.01") else target_price

        return target_price

    @staticmethod
    def _clamp_price(price: Decimal) -> Decimal:
        price = max(Decimal("0.01"), min(price, Decimal("0.85")))
        return price.quantize(Decimal("0.001"), rounding=ROUND_DOWN)

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    async def place_limit_orders(self, market: MarketInfo):
        """Размещаем лимитные ордера YES и NO для фарма поинтов."""
        if self.config.stop_new_orders_after_fill and market.id in self.filled_markets:
            logger.info(f"  Пропуск {market.title[:40]}: позиция уже открыта")
            return

        existing = [o for o in self.active_orders.values() if o.market_id == market.id]
        if len(existing) >= 2:
            logger.debug(f"  Ордера уже стоят на {market.title[:40]}")
            return

        price_yes, price_no = await self.calculate_passive_prices(market)

        yes_token = market.token_ids.get(0)
        no_token = market.token_ids.get(1)

        if yes_token:
            await self._send_limit_order(
                market=market,
                outcome_index=0,
                token_id=yes_token,
                price=price_yes,
            )

        if no_token:
            await self._send_limit_order(
                market=market,
                outcome_index=1,
                token_id=no_token,
                price=price_no,
            )

    async def _send_limit_order(
        self,
        market: MarketInfo,
        outcome_index: int,
        token_id: str,
        price: Decimal,
    ):
        side_label = "YES" if outcome_index == 0 else "NO"

        try:
            price_wei = int(price * WEI)
            quantity_usd = self.config.order_size_usd
            quantity_wei = int(Decimal(str(quantity_usd)) / price * WEI)

            amounts = self.builder.get_limit_order_amounts(
                LimitHelperInput(
                    side=Side.BUY,
                    price_per_share_wei=price_wei,
                    quantity_wei=quantity_wei,
                )
            )

            order = self.builder.build_order(
                "LIMIT",
                BuildOrderInput(
                    side=Side.BUY,
                    token_id=token_id,
                    maker_amount=str(amounts.maker_amount),
                    taker_amount=str(amounts.taker_amount),
                    fee_rate_bps=market.fee_rate_bps,
                ),
            )

            typed_data = self.builder.build_typed_data(
                order,
                is_neg_risk=market.is_neg_risk,
                is_yield_bearing=market.is_yield_bearing,
            )
            signed = self.builder.sign_typed_data_order(typed_data)

            payload = {
                "data": {
                    "pricePerShare": str(price_wei),
                    "strategy": "LIMIT",
                    "slippageBps": "0",
                    "order": {
                        "salt": str(order.salt),
                        "maker": order.maker,
                        "signer": order.signer,
                        "taker": order.taker,
                        "tokenId": token_id,
                        "makerAmount": str(amounts.maker_amount),
                        "takerAmount": str(amounts.taker_amount),
                        "expiration": str(order.expiration),
                        "nonce": str(order.nonce),
                        "feeRateBps": str(market.fee_rate_bps),
                        "side": 0,  # BUY
                        "signatureType": order.signature_type if hasattr(order, "signature_type") else 2,
                        "signature": signed.signature,
                    },
                }
            }

            result = await self.api.submit_order(payload)

            order_hash = signed.hash if hasattr(signed, "hash") else str(uuid.uuid4())

            self.active_orders[order_hash] = ActiveOrder(
                order_hash=order_hash,
                market_id=market.id,
                outcome_index=outcome_index,
                price=price,
                size_usd=quantity_usd,
                placed_at=datetime.now(),
                token_id=token_id,
            )

            logger.info(
                f"  -> Ордер {side_label} @ {price:.3f} "
                f"(${quantity_usd}) на '{market.title[:30]}'"
            )

        except Exception as e:
            logger.error(f"  Ошибка размещения {side_label} ордера: {e}")

    # ------------------------------------------------------------------
    # Price Protection — автоотмена ордеров
    # ------------------------------------------------------------------

    async def price_protection_check(self) -> int:
        """
        Проверяем все активные ордера: если текущая цена рынка
        подошла ближе чем price_proximity_threshold — отменяем.
        """
        if not self.config.cancel_when_price_close:
            return 0
        if not self.active_orders:
            return 0

        cancelled = 0
        market_cache: dict[int, float | None] = {}
        to_remove: list[str] = []

        for order_hash, order in self.active_orders.items():
            mid = market_cache.get(order.market_id)
            if mid is None:
                raw = await self.api.get_market(order.market_id)
                if raw:
                    mid = self._extract_chance(raw)
                market_cache[order.market_id] = mid

            if mid is None:
                continue

            current_mid = Decimal(str(mid))
            if order.outcome_index == 1:
                current_mid = Decimal("1") - current_mid

            distance = abs(current_mid - order.price)
            threshold = Decimal(str(self.config.price_proximity_threshold))

            if distance < threshold:
                logger.warning(
                    f"  ЗАЩИТА: цена ({float(current_mid):.3f}) слишком близко "
                    f"к ордеру {order.order_hash[:12]}... @ {order.price:.3f} "
                    f"(расстояние {float(distance):.4f} < порог {float(threshold):.3f}). Отмена!"
                )
                to_remove.append(order_hash)

        if to_remove:
            try:
                await self.api.remove_orders(to_remove)
            except Exception as e:
                logger.error(f"  Ошибка при отмене ордеров через API: {e}")

            for h in to_remove:
                self.active_orders.pop(h, None)
                cancelled += 1

        if cancelled:
            logger.info(f"  Price Protection: отменено {cancelled} ордер(ов)")

        return cancelled

    # ------------------------------------------------------------------
    # Cleanup stale orders
    # ------------------------------------------------------------------

    async def cleanup_stale_orders(self, max_age_minutes: int = 60):
        """Отменяем ордера старше max_age_minutes."""
        now = datetime.now()
        stale = [
            h for h, o in self.active_orders.items()
            if (now - o.placed_at).total_seconds() > max_age_minutes * 60
        ]
        if not stale:
            return

        logger.info(f"  Очистка {len(stale)} устаревших ордеров (>{max_age_minutes} мин)")
        try:
            await self.api.remove_orders(stale)
        except Exception as e:
            logger.error(f"  Ошибка отмены устаревших ордеров: {e}")

        for h in stale:
            self.active_orders.pop(h, None)

    # ------------------------------------------------------------------
    # Sync with API — актуализация active_orders
    # ------------------------------------------------------------------

    async def sync_orders_with_api(self):
        """Синхронизируем локальное состояние с API (убираем заполненные)."""
        try:
            api_orders = await self.api.get_open_orders("OPEN")
            api_hashes = {o["hash"] for o in api_orders}

            removed = []
            for h in list(self.active_orders.keys()):
                if h not in api_hashes:
                    order = self.active_orders.pop(h)
                    self.filled_markets.add(order.market_id)
                    removed.append(h)

            if removed:
                logger.info(f"  Синхронизация: {len(removed)} ордер(ов) исполнено/отменено")

        except Exception as e:
            logger.warning(f"  Не удалось синхронизировать ордера: {e}")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self):
        self._running = True
        logger.info("=" * 60)
        logger.info("  Predict.fun Bot — режим PASSIVE_POINTS")
        logger.info(f"  Спред: {self.config.passive_spread*100:.0f}%")
        logger.info(f"  Защита: порог {self.config.price_proximity_threshold*100:.0f}%")
        logger.info(f"  Размер ордера: ${self.config.order_size_usd}")
        logger.info(f"  Фильтр вероятности: {self.config.min_probability}-{self.config.max_probability}")
        logger.info("=" * 60)

        try:
            self.builder.set_approvals()
            logger.info("  Approvals установлены")
        except Exception as e:
            logger.warning(f"  Не удалось установить approvals (возможно уже есть): {e}")

        last_rebalance = datetime.now() - timedelta(seconds=self.config.rebalance_interval_sec)
        cycle = 0

        while self._running:
            cycle += 1
            try:
                now = datetime.now()

                await self.price_protection_check()

                if cycle % 10 == 0:
                    await self.sync_orders_with_api()
                    await self.cleanup_stale_orders()

                if (now - last_rebalance).total_seconds() >= self.config.rebalance_interval_sec:
                    logger.info("-" * 40)
                    logger.info(f"  Цикл #{cycle}: поиск рынков...")

                    markets = await self.get_suitable_markets()
                    for market in markets[:self.config.max_active_markets]:
                        await self.place_limit_orders(market)

                    last_rebalance = now
                    logger.info(
                        f"  Активных ордеров: {len(self.active_orders)}, "
                        f"рынков с позицией: {len(self.filled_markets)}"
                    )

                await asyncio.sleep(self.config.price_check_interval_sec)

            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"  Ошибка в основном цикле: {e}", exc_info=True)
                await asyncio.sleep(10)

        await self.shutdown()

    async def shutdown(self):
        logger.info("Остановка бота...")
        self._running = False

        if self.active_orders:
            logger.info(f"  Отмена {len(self.active_orders)} активных ордеров...")
            try:
                await self.api.remove_orders(list(self.active_orders.keys()))
            except Exception as e:
                logger.error(f"  Ошибка при отмене ордеров: {e}")

        await self.api.close()
        logger.info("Бот остановлен.")


# ---------------------------------------------------------------------------
# Config loader
# ---------------------------------------------------------------------------

def load_config() -> BotConfig:
    load_dotenv()
    return BotConfig(
        chain_id=os.getenv("CHAIN_ID", "BNB_MAINNET"),
        min_liquidity_usd=float(os.getenv("MIN_LIQUIDITY_USD", "1000.0")),
        max_liquidity_usd=float(os.getenv("MAX_LIQUIDITY_USD", "25000.0")),
        min_probability=float(os.getenv("MIN_PROBABILITY", "0.45")),
        max_probability=float(os.getenv("MAX_PROBABILITY", "0.55")),
        max_active_markets=int(os.getenv("MAX_ACTIVE_MARKETS", "3")),
        max_position_usd=float(os.getenv("MAX_POSITION_USD", "100.0")),
        order_size_usd=float(os.getenv("ORDER_SIZE_USD", "20.0")),
        strategy_mode=os.getenv("STRATEGY_MODE", "PASSIVE_POINTS"),
        passive_spread=float(os.getenv("PASSIVE_SPREAD", "0.15")),
        cancel_when_price_close=os.getenv("CANCEL_WHEN_PRICE_CLOSE", "true").lower() == "true",
        price_proximity_threshold=float(os.getenv("PRICE_PROXIMITY_THRESHOLD", "0.03")),
        price_check_interval_sec=int(os.getenv("PRICE_CHECK_INTERVAL_SEC", "15")),
        rebalance_interval_sec=int(os.getenv("REBALANCE_INTERVAL_SEC", "300")),
        stop_new_orders_after_fill=os.getenv("STOP_NEW_ORDERS_AFTER_FILL", "true").lower() == "true",
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    config = load_config()

    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    private_key = os.getenv("PRIVATE_KEY")
    if not private_key:
        logger.error("PRIVATE_KEY не задан в .env")
        sys.exit(1)

    predict_account = os.getenv("PREDICT_ACCOUNT")
    api_key = os.getenv("PREDICT_API_KEY")

    bot = MarketMakerBot(
        config=config,
        private_key=private_key,
        predict_account=predict_account,
        api_key=api_key,
    )

    loop = asyncio.new_event_loop()

    def _signal_handler():
        bot._running = False

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    try:
        loop.run_until_complete(bot.run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
