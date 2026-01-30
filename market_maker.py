#!/usr/bin/env python3
"""
================================================================================
Predict.fun Market Making Bot — Liquidity Provider для фарма Predict Points
================================================================================

Описание / Description:
-----------------------
Этот скрипт реализует стратегию предоставления ликвидности через лимит-ордера
на платформе Predict.fun (BNB Chain). Цель — заработать Predict Points за счёт
2x мультипликатора в "uncertain markets" (рынки с вероятностью 40-60%).

What this script does:
- Places bid/ask limit orders on both YES and NO outcomes
- Maintains delta-neutral position (equal exposure on both sides)
- Automatically rebalances orders when price moves
- Farms Predict Points through liquidity provision (2x multiplier)

ВАЖНЫЕ ПРЕДУПРЕЖДЕНИЯ / WARNINGS:
---------------------------------
⚠️  НЕ ЯВЛЯЕТСЯ ФИНАНСОВОЙ РЕКОМЕНДАЦИЕЙ / NOT FINANCIAL ADVICE
⚠️  Риски исполнения ордеров в убыток при резких движениях цены
⚠️  Газ на BNB Chain может съедать прибыль на малых объёмах
⚠️  Правила платформы могут измениться, боты могут быть запрещены
⚠️  Анти-сибил: не используйте множество аккаунтов
⚠️  ТЕСТИРУЙТЕ НА МАЛЫХ СУММАХ ($100-500) ПЕРЕД МАСШТАБИРОВАНИЕМ

Документация:
- https://dev.predict.fun/
- https://github.com/PredictDotFun/sdk-python

================================================================================
"""

import asyncio
import logging
import os
import sys
import time
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Optional, Literal

import aiohttp

# Third-party imports
try:
    from dotenv import load_dotenv
except ImportError:
    print("❌ Установите python-dotenv: pip install python-dotenv")
    sys.exit(1)

try:
    from predict_sdk import (
        OrderBuilder,
        OrderBuilderOptions,
        ChainId,
        Side,
        BuildOrderInput,
        LimitHelperInput,
        SignedOrder,
    )
except ImportError:
    print("❌ Установите predict-sdk: pip install predict-sdk==0.0.12")
    print("   Документация: https://github.com/PredictDotFun/sdk-python")
    sys.exit(1)

try:
    from eth_account import Account
except ImportError:
    print("❌ Установите web3: pip install web3")
    sys.exit(1)


# ================================================================================
# CONSTANTS / КОНСТАНТЫ
# ================================================================================

# Predict.fun REST API
PREDICT_API_BASE = "https://api.predict.fun"
PREDICT_API_V1 = f"{PREDICT_API_BASE}/api/v1"

# Wei conversion (18 decimals for most tokens)
WEI_DECIMALS = 18
WEI_MULTIPLIER = 10 ** WEI_DECIMALS

# USDC/USDT typically has 6 decimals
USDC_DECIMALS = 6
USDC_MULTIPLIER = 10 ** USDC_DECIMALS


# ================================================================================
# CONFIGURATION / КОНФИГУРАЦИЯ
# ================================================================================

@dataclass
class BotConfig:
    """
    Конфигурация бота / Bot configuration
    Все параметры можно переопределить через переменные окружения
    """
    # Chain settings / Настройки сети
    chain_id: ChainId = ChainId.BNB_MAINNET
    
    # Market filters / Фильтры рынков
    min_oi_usd: float = 0.0          # Минимальный Open Interest ($)
    max_oi_usd: float = 15000.0      # Максимальный Open Interest ($) — низкий OI = больше поинтов
    min_probability: float = 0.40    # Минимальная вероятность (40%)
    max_probability: float = 0.60    # Максимальная вероятность (60%) — "uncertain markets" 2x
    
    # Order settings / Настройки ордеров
    target_spread: float = 0.03      # Целевой спред от mid-price (±3%)
    order_size_usd: float = 25.0     # Размер каждого ордера в USD
    min_order_size_usd: float = 5.0  # Минимальный размер ордера
    max_orders_per_market: int = 2   # Макс. ордеров на сторону (YES/NO)
    
    # Timing / Тайминги
    rebalance_interval_sec: int = 300   # Интервал ребалансировки (5 минут)
    price_change_threshold: float = 0.02  # Порог изменения цены для ребалансировки
    order_expiry_minutes: int = 60       # Время жизни ордера (минуты)
    
    # Safety / Безопасность
    max_total_exposure_usd: float = 500.0  # Макс. общая позиция в USD
    max_slippage: float = 0.05             # Макс. проскальзывание (5%)
    
    # Rate limits
    api_delay_sec: float = 0.5             # Задержка между API вызовами
    
    # Market IDs to trade (empty = auto-select) / ID рынков для торговли
    market_ids: list[str] = field(default_factory=list)
    
    # Predict Account (smart wallet address)
    predict_account: Optional[str] = None
    
    # Fee rate in basis points (default 0 for maker orders)
    fee_rate_bps: int = 0
    
    # Logging
    log_level: str = "INFO"


class OrderStatus(Enum):
    """Статусы ордеров"""
    PENDING = "pending"
    OPEN = "open"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass
class MarketData:
    """Данные рынка из API"""
    market_id: str
    question_id: str
    title: str
    yes_token_id: str
    no_token_id: str
    yes_price: Decimal
    no_price: Decimal
    open_interest_usd: Decimal
    volume_24h_usd: Decimal
    end_date: Optional[datetime]
    neg_risk: bool = False


@dataclass
class OrderbookData:
    """Данные ордербука"""
    yes_best_bid: Optional[Decimal]
    yes_best_ask: Optional[Decimal]
    no_best_bid: Optional[Decimal]
    no_best_ask: Optional[Decimal]
    yes_mid_price: Decimal
    no_mid_price: Decimal


@dataclass
class OrderInfo:
    """Информация об ордере"""
    order_hash: str
    market_id: str
    token_id: str
    side: Side  # BUY or SELL
    price: Decimal
    size_wei: int
    status: OrderStatus
    created_at: datetime
    expires_at: datetime


@dataclass
class MarketState:
    """Состояние рынка"""
    market: MarketData
    orderbook: Optional[OrderbookData] = None
    our_orders: list[OrderInfo] = field(default_factory=list)
    last_rebalance: Optional[datetime] = None


# ================================================================================
# LOGGING SETUP / НАСТРОЙКА ЛОГИРОВАНИЯ
# ================================================================================

def setup_logging(level: str = "INFO") -> logging.Logger:
    """
    Настройка логирования / Setup logging
    """
    logger = logging.getLogger("PredictMM")
    logger.setLevel(getattr(logging, level.upper()))
    
    # Remove existing handlers
    logger.handlers.clear()
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    
    # File handler
    file_handler = logging.FileHandler(
        f"predict_mm_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )
    file_handler.setLevel(logging.DEBUG)
    
    # Format
    formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_handler.setFormatter(formatter)
    file_handler.setFormatter(formatter)
    
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    
    return logger


# ================================================================================
# PREDICT API CLIENT / КЛИЕНТ PREDICT API
# ================================================================================

class PredictAPIClient:
    """
    REST API клиент для Predict.fun
    Получает данные о рынках, ордербуках, отправляет ордера
    
    Документация: https://dev.predict.fun/
    """
    
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession()
        return self
    
    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()
    
    async def _request(
        self, 
        method: str, 
        endpoint: str, 
        params: dict = None,
        json_data: dict = None
    ) -> dict:
        """Make HTTP request to API"""
        if not self.session:
            self.session = aiohttp.ClientSession()
        
        url = f"{PREDICT_API_V1}{endpoint}"
        
        try:
            async with self.session.request(
                method, 
                url, 
                params=params, 
                json=json_data,
                headers={"Content-Type": "application/json"}
            ) as response:
                data = await response.json()
                
                if response.status >= 400:
                    self.logger.error(f"API Error {response.status}: {data}")
                    raise Exception(f"API Error: {data}")
                
                return data
                
        except aiohttp.ClientError as e:
            self.logger.error(f"HTTP Error: {e}")
            raise
    
    async def get_markets(
        self, 
        status: str = "active",
        limit: int = 100,
        offset: int = 0
    ) -> list[MarketData]:
        """
        Получить список рынков / Get list of markets
        
        GET /api/v1/markets
        """
        params = {
            "status": status,
            "limit": limit,
            "offset": offset
        }
        
        data = await self._request("GET", "/markets", params=params)
        
        markets = []
        for item in data.get("markets", data.get("data", [])):
            try:
                # Парсим данные рынка
                market = MarketData(
                    market_id=str(item.get("id") or item.get("marketId")),
                    question_id=str(item.get("questionId", "")),
                    title=item.get("title", item.get("question", "Unknown")),
                    yes_token_id=str(item.get("yesTokenId", item.get("outcomes", [{}])[0].get("tokenId", ""))),
                    no_token_id=str(item.get("noTokenId", item.get("outcomes", [{}])[-1].get("tokenId", "") if len(item.get("outcomes", [])) > 1 else "")),
                    yes_price=Decimal(str(item.get("yesPrice", item.get("lastPrice", 0.5)))),
                    no_price=Decimal(str(item.get("noPrice", 1 - float(item.get("yesPrice", item.get("lastPrice", 0.5)))))),
                    open_interest_usd=Decimal(str(item.get("openInterestUsd", item.get("openInterest", 0)))),
                    volume_24h_usd=Decimal(str(item.get("volume24hUsd", item.get("volume24h", 0)))),
                    end_date=None,  # Parse if available
                    neg_risk=item.get("negRisk", False)
                )
                markets.append(market)
            except Exception as e:
                self.logger.debug(f"Error parsing market: {e}")
                continue
        
        return markets
    
    async def get_market(self, market_id: str) -> Optional[MarketData]:
        """
        Получить данные одного рынка / Get single market data
        
        GET /api/v1/markets/{market_id}
        """
        try:
            data = await self._request("GET", f"/markets/{market_id}")
            item = data.get("market", data)
            
            return MarketData(
                market_id=str(item.get("id") or item.get("marketId")),
                question_id=str(item.get("questionId", "")),
                title=item.get("title", item.get("question", "Unknown")),
                yes_token_id=str(item.get("yesTokenId", "")),
                no_token_id=str(item.get("noTokenId", "")),
                yes_price=Decimal(str(item.get("yesPrice", 0.5))),
                no_price=Decimal(str(item.get("noPrice", 0.5))),
                open_interest_usd=Decimal(str(item.get("openInterestUsd", 0))),
                volume_24h_usd=Decimal(str(item.get("volume24hUsd", 0))),
                end_date=None,
                neg_risk=item.get("negRisk", False)
            )
        except Exception as e:
            self.logger.error(f"Error fetching market {market_id}: {e}")
            return None
    
    async def get_orderbook(self, market_id: str) -> Optional[OrderbookData]:
        """
        Получить ордербук рынка / Get market orderbook
        
        GET /api/v1/markets/{market_id}/orderbook
        """
        try:
            data = await self._request("GET", f"/markets/{market_id}/orderbook")
            
            # Parse bids and asks
            yes_bids = data.get("yesBids", data.get("bids", []))
            yes_asks = data.get("yesAsks", data.get("asks", []))
            no_bids = data.get("noBids", [])
            no_asks = data.get("noAsks", [])
            
            yes_best_bid = Decimal(str(yes_bids[0]["price"])) if yes_bids else None
            yes_best_ask = Decimal(str(yes_asks[0]["price"])) if yes_asks else None
            no_best_bid = Decimal(str(no_bids[0]["price"])) if no_bids else None
            no_best_ask = Decimal(str(no_asks[0]["price"])) if no_asks else None
            
            # Calculate mid prices
            if yes_best_bid and yes_best_ask:
                yes_mid = (yes_best_bid + yes_best_ask) / 2
            elif yes_best_bid:
                yes_mid = yes_best_bid
            elif yes_best_ask:
                yes_mid = yes_best_ask
            else:
                yes_mid = Decimal("0.5")
            
            no_mid = Decimal("1") - yes_mid
            
            return OrderbookData(
                yes_best_bid=yes_best_bid,
                yes_best_ask=yes_best_ask,
                no_best_bid=no_best_bid,
                no_best_ask=no_best_ask,
                yes_mid_price=yes_mid,
                no_mid_price=no_mid
            )
            
        except Exception as e:
            self.logger.warning(f"Error fetching orderbook for {market_id}: {e}")
            return None
    
    async def submit_order(self, signed_order: dict) -> dict:
        """
        Отправить подписанный ордер / Submit signed order
        
        POST /api/v1/orders
        """
        return await self._request("POST", "/orders", json_data=signed_order)
    
    async def cancel_order(self, order_hash: str) -> dict:
        """
        Отменить ордер / Cancel order
        
        DELETE /api/v1/orders/{order_hash}
        """
        return await self._request("DELETE", f"/orders/{order_hash}")
    
    async def get_orders(self, maker: str, status: str = "open") -> list[dict]:
        """
        Получить ордера пользователя / Get user orders
        
        GET /api/v1/orders
        """
        params = {"maker": maker, "status": status}
        data = await self._request("GET", "/orders", params=params)
        return data.get("orders", data.get("data", []))


# ================================================================================
# MARKET MAKER BOT / MARKET MAKING БОТ
# ================================================================================

class MarketMakerBot:
    """
    Market Making Bot для Predict.fun
    
    Основной класс, реализующий стратегию предоставления ликвидности
    через размещение лимит-ордеров с обеих сторон рынка.
    
    Main class implementing liquidity provision strategy through
    placing limit orders on both sides of the market.
    """
    
    def __init__(self, config: BotConfig, private_key: str):
        """
        Инициализация бота / Initialize bot
        
        Args:
            config: Конфигурация бота
            private_key: Приватный ключ кошелька (НЕ хардкодить!)
        """
        self.config = config
        self.logger = setup_logging(config.log_level)
        
        # Initialize Account from private key
        self.account = Account.from_key(private_key)
        self.address = self.account.address
        self.logger.info(f"🔑 Wallet initialized: {self.address[:10]}...{self.address[-6:]}")
        
        # Predict Account (smart wallet) - if provided
        self.predict_account = config.predict_account
        if self.predict_account:
            self.logger.info(f"📱 Predict Account: {self.predict_account[:10]}...{self.predict_account[-6:]}")
        
        # Initialize Predict SDK OrderBuilder
        # Используем официальный SDK для построения и подписания ордеров
        options = OrderBuilderOptions(
            precision=WEI_DECIMALS,
            predict_account=self.predict_account,
            log_level=config.log_level
        )
        
        self.order_builder = OrderBuilder.make(
            chain_id=config.chain_id,
            signer=private_key,
            options=options
        )
        
        # API Client for REST endpoints
        self.api_client = PredictAPIClient(self.logger)
        
        # State tracking / Отслеживание состояния
        self.markets: dict[str, MarketState] = {}
        self.active_orders: dict[str, OrderInfo] = {}
        self.total_pnl: Decimal = Decimal("0")
        self.total_points_earned: Decimal = Decimal("0")
        
        # Statistics / Статистика
        self.orders_placed: int = 0
        self.orders_filled: int = 0
        self.orders_cancelled: int = 0
        
        # Running flag
        self._running: bool = False
        
        self.logger.info("✅ MarketMakerBot initialized successfully")
    
    async def get_suitable_markets(self) -> list[MarketData]:
        """
        Получить подходящие рынки для маркет-мейкинга
        Get suitable markets for market making
        
        Фильтрует рынки по критериям:
        - Open Interest < max_oi_usd (низкий OI = больше поинтов)
        - Вероятность 40-60% (uncertain markets = 2x мультипликатор)
        - Рынок активен и принимает ордера
        
        Returns:
            List of suitable MarketData objects
        """
        self.logger.info("🔍 Searching for suitable markets...")
        
        try:
            # Получаем список всех активных рынков через REST API
            all_markets = await self.api_client.get_markets(status="active", limit=100)
            
            suitable = []
            
            for market in all_markets:
                # Пропускаем, если указаны конкретные market_ids
                if self.config.market_ids and market.market_id not in self.config.market_ids:
                    continue
                
                # Проверяем Open Interest
                oi_usd = float(market.open_interest_usd)
                if oi_usd > self.config.max_oi_usd:
                    self.logger.debug(f"  ⏭️  {market.title[:40]}... - OI too high: ${oi_usd:,.0f}")
                    continue
                if oi_usd < self.config.min_oi_usd:
                    self.logger.debug(f"  ⏭️  {market.title[:40]}... - OI too low: ${oi_usd:,.0f}")
                    continue
                
                # Проверяем вероятность (цена YES = вероятность)
                probability = float(market.yes_price)
                if not (self.config.min_probability <= probability <= self.config.max_probability):
                    self.logger.debug(
                        f"  ⏭️  {market.title[:40]}... - "
                        f"probability {probability:.1%} outside range"
                    )
                    continue
                
                # Проверяем наличие token IDs
                if not market.yes_token_id or not market.no_token_id:
                    self.logger.debug(f"  ⏭️  {market.title[:40]}... - missing token IDs")
                    continue
                
                # Рынок подходит! / Market is suitable!
                suitable.append(market)
                self.logger.info(
                    f"  ✅ {market.title[:50]}... | "
                    f"OI: ${oi_usd:,.0f} | Prob: {probability:.1%}"
                )
                
                # Rate limiting
                await asyncio.sleep(self.config.api_delay_sec)
            
            self.logger.info(f"📊 Found {len(suitable)} suitable markets")
            return suitable
            
        except Exception as e:
            self.logger.error(f"❌ Error fetching markets: {e}")
            return []
    
    def calculate_order_params(
        self, 
        mid_price: Decimal,
        order_size_usd: Optional[float] = None
    ) -> tuple[Decimal, Decimal, int, int]:
        """
        Рассчитать параметры ордеров (bid/ask цены и размеры)
        Calculate order parameters (bid/ask prices and sizes)
        
        Args:
            mid_price: Средняя цена (YES probability)
            order_size_usd: Размер ордера в USD (опционально)
            
        Returns:
            Tuple of (bid_price, ask_price, bid_size_wei, ask_size_wei)
        """
        spread = Decimal(str(self.config.target_spread))
        order_size = Decimal(str(order_size_usd or self.config.order_size_usd))
        
        # Bid ниже mid, Ask выше mid
        # Bid below mid, Ask above mid
        bid_price = mid_price - spread
        ask_price = mid_price + spread
        
        # Ограничиваем цены в диапазоне [0.01, 0.99]
        # Clamp prices to [0.01, 0.99]
        bid_price = max(Decimal("0.01"), min(Decimal("0.99"), bid_price))
        ask_price = max(Decimal("0.01"), min(Decimal("0.99"), ask_price))
        
        # Размер в wei = (USD * 10^6) / price (для USDC с 6 decimals)
        # Predict использует shares с precision 18
        bid_size_wei = int((order_size / bid_price) * WEI_MULTIPLIER)
        ask_size_wei = int((order_size / ask_price) * WEI_MULTIPLIER)
        
        return bid_price, ask_price, bid_size_wei, ask_size_wei
    
    def price_to_wei(self, price: Decimal) -> int:
        """Convert price (0-1) to wei (price per share in wei)"""
        return int(price * WEI_MULTIPLIER)
    
    async def build_and_sign_order(
        self,
        token_id: str,
        side: Side,
        price: Decimal,
        quantity_wei: int,
    ) -> Optional[dict]:
        """
        Построить и подписать ордер используя SDK
        Build and sign order using SDK
        
        Args:
            token_id: ID токена (YES или NO)
            side: Side.BUY или Side.SELL
            price: Цена за share (0-1)
            quantity_wei: Количество shares в wei
            
        Returns:
            Signed order dict ready for submission, or None on error
        """
        try:
            # Рассчитываем amounts через SDK helper
            price_wei = self.price_to_wei(price)
            
            limit_input = LimitHelperInput(
                side=side,
                price_per_share_wei=price_wei,
                quantity_wei=quantity_wei
            )
            
            amounts = self.order_builder.get_limit_order_amounts(limit_input)
            
            # Строим ордер
            expires_at = datetime.now() + timedelta(minutes=self.config.order_expiry_minutes)
            
            order_input = BuildOrderInput(
                side=side,
                token_id=token_id,
                maker_amount=str(amounts.maker),
                taker_amount=str(amounts.taker),
                fee_rate_bps=str(self.config.fee_rate_bps),
                expires_at=expires_at
            )
            
            order = self.order_builder.build_order(strategy="LIMIT", data=order_input)
            
            # Подписываем ордер
            signed_order = self.order_builder.sign_typed_data_order(order)
            
            return {
                "order": {
                    "salt": str(order.salt),
                    "maker": order.maker,
                    "signer": order.signer,
                    "taker": order.taker,
                    "tokenId": str(order.token_id),
                    "makerAmount": str(order.maker_amount),
                    "takerAmount": str(order.taker_amount),
                    "expiration": str(int(order.expiration.timestamp())) if order.expiration else "0",
                    "nonce": str(order.nonce),
                    "feeRateBps": str(order.fee_rate_bps),
                    "side": "BUY" if side == Side.BUY else "SELL",
                    "signatureType": str(order.signature_type.value if order.signature_type else 0),
                },
                "signature": signed_order.signature,
                "orderHash": signed_order.order_hash if hasattr(signed_order, 'order_hash') else None
            }
            
        except Exception as e:
            self.logger.error(f"❌ Error building order: {e}")
            return None
    
    async def place_limit_orders(self, market: MarketData) -> list[OrderInfo]:
        """
        Разместить лимит-ордера на обеих сторонах рынка
        Place limit orders on both sides of the market
        
        Стратегия delta-neutral: равный объём на YES и NO
        Delta-neutral strategy: equal volume on YES and NO
        
        Args:
            market: MarketData object
            
        Returns:
            List of placed OrderInfo objects
        """
        self.logger.info(f"📝 Placing orders for: {market.title[:50]}...")
        placed_orders = []
        
        try:
            # Получаем текущий orderbook / Get current orderbook
            orderbook = await self.api_client.get_orderbook(market.market_id)
            
            if orderbook:
                yes_mid = orderbook.yes_mid_price
            else:
                yes_mid = market.yes_price
            
            # Рассчитываем параметры ордеров
            bid_price, ask_price, bid_size_wei, ask_size_wei = self.calculate_order_params(yes_mid)
            
            self.logger.info(
                f"  📊 Mid: {yes_mid:.4f} | "
                f"Bid: {bid_price:.4f} | "
                f"Ask: {ask_price:.4f}"
            )
            
            # ----------------------------------------------------------------
            # Размещаем ордер на покупку YES (bid)
            # Place YES buy order (bid)
            # ----------------------------------------------------------------
            if market.yes_token_id:
                try:
                    signed_order = await self.build_and_sign_order(
                        token_id=market.yes_token_id,
                        side=Side.BUY,
                        price=bid_price,
                        quantity_wei=bid_size_wei
                    )
                    
                    if signed_order:
                        result = await self.api_client.submit_order(signed_order)
                        
                        order_info = OrderInfo(
                            order_hash=result.get("orderHash", signed_order.get("orderHash", "")),
                            market_id=market.market_id,
                            token_id=market.yes_token_id,
                            side=Side.BUY,
                            price=bid_price,
                            size_wei=bid_size_wei,
                            status=OrderStatus.OPEN,
                            created_at=datetime.now(),
                            expires_at=datetime.now() + timedelta(minutes=self.config.order_expiry_minutes)
                        )
                        placed_orders.append(order_info)
                        self.active_orders[order_info.order_hash] = order_info
                        self.orders_placed += 1
                        
                        self.logger.info(f"  ✅ YES BUY order placed: {bid_price:.4f}")
                        
                except Exception as e:
                    self.logger.error(f"  ❌ Failed to place YES BUY order: {e}")
            
            await asyncio.sleep(self.config.api_delay_sec)
            
            # ----------------------------------------------------------------
            # Размещаем ордер на покупку NO (для delta-neutral)
            # Place NO buy order (for delta-neutral)
            # NO price = 1 - YES ask price (we want to buy NO when YES is expensive)
            # ----------------------------------------------------------------
            if market.no_token_id:
                try:
                    no_bid_price = Decimal("1") - ask_price  # Инвертируем для NO
                    no_bid_price = max(Decimal("0.01"), min(Decimal("0.99"), no_bid_price))
                    
                    signed_order = await self.build_and_sign_order(
                        token_id=market.no_token_id,
                        side=Side.BUY,
                        price=no_bid_price,
                        quantity_wei=ask_size_wei
                    )
                    
                    if signed_order:
                        result = await self.api_client.submit_order(signed_order)
                        
                        order_info = OrderInfo(
                            order_hash=result.get("orderHash", signed_order.get("orderHash", "")),
                            market_id=market.market_id,
                            token_id=market.no_token_id,
                            side=Side.BUY,
                            price=no_bid_price,
                            size_wei=ask_size_wei,
                            status=OrderStatus.OPEN,
                            created_at=datetime.now(),
                            expires_at=datetime.now() + timedelta(minutes=self.config.order_expiry_minutes)
                        )
                        placed_orders.append(order_info)
                        self.active_orders[order_info.order_hash] = order_info
                        self.orders_placed += 1
                        
                        self.logger.info(f"  ✅ NO BUY order placed: {no_bid_price:.4f}")
                        
                except Exception as e:
                    self.logger.error(f"  ❌ Failed to place NO BUY order: {e}")
            
            # Обновляем состояние рынка / Update market state
            if market.market_id not in self.markets:
                self.markets[market.market_id] = MarketState(market=market)
            
            self.markets[market.market_id].orderbook = orderbook
            self.markets[market.market_id].our_orders = placed_orders
            self.markets[market.market_id].last_rebalance = datetime.now()
            
            return placed_orders
            
        except Exception as e:
            self.logger.error(f"❌ Error placing orders: {e}")
            return []
    
    async def cancel_old_orders(self, market_id: Optional[str] = None) -> int:
        """
        Отменить старые ордера перед ребалансировкой
        Cancel old orders before rebalancing
        
        Args:
            market_id: Optional market ID to filter orders
            
        Returns:
            Number of cancelled orders
        """
        cancelled_count = 0
        orders_to_cancel = []
        
        for order_hash, order in self.active_orders.items():
            if market_id and order.market_id != market_id:
                continue
            if order.status == OrderStatus.OPEN:
                orders_to_cancel.append(order_hash)
        
        for order_hash in orders_to_cancel:
            try:
                await self.api_client.cancel_order(order_hash)
                self.active_orders[order_hash].status = OrderStatus.CANCELLED
                cancelled_count += 1
                self.orders_cancelled += 1
                self.logger.info(f"  🗑️  Cancelled order: {order_hash[:16]}...")
                
            except Exception as e:
                self.logger.warning(f"  ⚠️  Failed to cancel order {order_hash}: {e}")
            
            await asyncio.sleep(self.config.api_delay_sec)
        
        return cancelled_count
    
    async def check_and_update_orders(self) -> None:
        """
        Проверить статус ордеров и обновить PnL
        Check order status and update PnL
        """
        try:
            maker_address = self.predict_account or self.address
            open_orders = await self.api_client.get_orders(maker_address, "open")
            filled_orders = await self.api_client.get_orders(maker_address, "filled")
            
            open_hashes = {o.get("orderHash") for o in open_orders}
            filled_hashes = {o.get("orderHash") for o in filled_orders}
            
            for order_hash, order in list(self.active_orders.items()):
                if order.status != OrderStatus.OPEN:
                    continue
                
                if order_hash in filled_hashes:
                    order.status = OrderStatus.FILLED
                    self.orders_filled += 1
                    self.logger.info(
                        f"  💰 Order FILLED: {'YES' if 'yes' in order.token_id.lower() else 'NO'} "
                        f"{order.side.name} | Price: {order.price:.4f}"
                    )
                elif order_hash not in open_hashes:
                    # Order might be cancelled or expired
                    if datetime.now() > order.expires_at:
                        order.status = OrderStatus.EXPIRED
                    else:
                        order.status = OrderStatus.CANCELLED
                        
        except Exception as e:
            self.logger.debug(f"  ⚠️  Error checking orders: {e}")
    
    async def should_rebalance(self, market_state: MarketState) -> bool:
        """
        Проверить, нужна ли ребалансировка
        Check if rebalancing is needed
        
        Returns:
            True if rebalancing is needed
        """
        # Проверяем время с последней ребалансировки
        if market_state.last_rebalance:
            time_since = (datetime.now() - market_state.last_rebalance).total_seconds()
            if time_since < self.config.rebalance_interval_sec:
                return False
        
        # Проверяем изменение цены
        try:
            orderbook = await self.api_client.get_orderbook(market_state.market.market_id)
            if orderbook and market_state.orderbook:
                current_mid = orderbook.yes_mid_price
                old_mid = market_state.orderbook.yes_mid_price
                
                price_change = abs(current_mid - old_mid)
                if price_change > Decimal(str(self.config.price_change_threshold)):
                    self.logger.info(
                        f"  📈 Price changed: {old_mid:.4f} → {current_mid:.4f} "
                        f"(Δ{price_change:.4f})"
                    )
                    return True
            
        except Exception as e:
            self.logger.warning(f"  ⚠️  Error checking price: {e}")
        
        return True  # Ребалансируем по таймеру / Rebalance on timer
    
    async def monitor_and_rebalance(self) -> None:
        """
        Основной цикл мониторинга и ребалансировки
        Main monitoring and rebalancing loop
        """
        self.logger.info("🔄 Starting monitor and rebalance loop...")
        
        while self._running:
            try:
                # Проверяем статус существующих ордеров
                await self.check_and_update_orders()
                
                # Проверяем каждый рынок на необходимость ребалансировки
                for market_id, market_state in list(self.markets.items()):
                    if await self.should_rebalance(market_state):
                        self.logger.info(f"🔄 Rebalancing: {market_state.market.title[:40]}...")
                        
                        # Отменяем старые ордера
                        cancelled = await self.cancel_old_orders(market_id)
                        self.logger.info(f"  🗑️  Cancelled {cancelled} old orders")
                        
                        # Получаем актуальную информацию о рынке
                        try:
                            updated_market = await self.api_client.get_market(market_id)
                            
                            if updated_market:
                                market_state.market = updated_market
                                
                                # Размещаем новые ордера
                                new_orders = await self.place_limit_orders(updated_market)
                                self.logger.info(f"  ✅ Placed {len(new_orders)} new orders")
                            
                        except Exception as e:
                            self.logger.error(f"  ❌ Error rebalancing market: {e}")
                        
                        await asyncio.sleep(self.config.api_delay_sec)
                
                # Логируем статистику
                self.log_statistics()
                
                # Ждём перед следующей проверкой
                await asyncio.sleep(self.config.rebalance_interval_sec)
                
            except asyncio.CancelledError:
                self.logger.info("⏹️  Monitor loop cancelled")
                break
            except Exception as e:
                self.logger.error(f"❌ Error in monitor loop: {e}")
                await asyncio.sleep(30)  # Wait before retry
    
    def log_statistics(self) -> None:
        """
        Логирование статистики / Log statistics
        """
        self.logger.info("=" * 60)
        self.logger.info("📊 STATISTICS / СТАТИСТИКА")
        self.logger.info("-" * 60)
        self.logger.info(f"  Markets active:    {len(self.markets)}")
        self.logger.info(f"  Orders placed:     {self.orders_placed}")
        self.logger.info(f"  Orders filled:     {self.orders_filled}")
        self.logger.info(f"  Orders cancelled:  {self.orders_cancelled}")
        self.logger.info(f"  Active orders:     {sum(1 for o in self.active_orders.values() if o.status == OrderStatus.OPEN)}")
        self.logger.info("=" * 60)
    
    async def run(self) -> None:
        """
        Запустить бота / Run the bot
        """
        self.logger.info("=" * 60)
        self.logger.info("🚀 PREDICT.FUN MARKET MAKER BOT")
        self.logger.info("=" * 60)
        self._running = True
        
        async with self.api_client:
            try:
                # 1. Получаем подходящие рынки / Get suitable markets
                markets = await self.get_suitable_markets()
                if not markets:
                    self.logger.warning("⚠️  No suitable markets found. Check filters or try later.")
                    return
                
                # 2. Размещаем начальные ордера / Place initial orders
                for market in markets[:5]:  # Ограничиваем 5 рынками для начала
                    await self.place_limit_orders(market)
                    await asyncio.sleep(self.config.api_delay_sec)
                
                # 3. Запускаем цикл мониторинга / Start monitoring loop
                await self.monitor_and_rebalance()
                
            except KeyboardInterrupt:
                self.logger.info("⏹️  Received shutdown signal...")
            except Exception as e:
                self.logger.error(f"❌ Fatal error: {e}")
                import traceback
                traceback.print_exc()
            finally:
                await self.shutdown()
    
    async def shutdown(self) -> None:
        """
        Graceful shutdown — отменить все ордера и завершить работу
        Graceful shutdown — cancel all orders and exit
        """
        self.logger.info("🛑 Initiating graceful shutdown...")
        self._running = False
        
        # Отменяем все открытые ордера
        cancelled = await self.cancel_old_orders()
        self.logger.info(f"🗑️  Cancelled {cancelled} orders on shutdown")
        
        # Финальная статистика
        self.log_statistics()
        
        self.logger.info("👋 Bot shutdown complete. Goodbye!")


# ================================================================================
# MAIN / ТОЧКА ВХОДА
# ================================================================================

def load_config() -> BotConfig:
    """
    Загрузить конфигурацию из переменных окружения
    Load configuration from environment variables
    """
    load_dotenv()
    
    config = BotConfig()
    
    # Override from environment / Переопределение из окружения
    if os.getenv("MIN_OI_USD"):
        config.min_oi_usd = float(os.getenv("MIN_OI_USD"))
    if os.getenv("MAX_OI_USD"):
        config.max_oi_usd = float(os.getenv("MAX_OI_USD"))
    if os.getenv("TARGET_SPREAD"):
        config.target_spread = float(os.getenv("TARGET_SPREAD"))
    if os.getenv("ORDER_SIZE_USD"):
        config.order_size_usd = float(os.getenv("ORDER_SIZE_USD"))
    if os.getenv("REBALANCE_INTERVAL_SEC"):
        config.rebalance_interval_sec = int(os.getenv("REBALANCE_INTERVAL_SEC"))
    if os.getenv("MAX_TOTAL_EXPOSURE_USD"):
        config.max_total_exposure_usd = float(os.getenv("MAX_TOTAL_EXPOSURE_USD"))
    if os.getenv("MARKET_IDS"):
        config.market_ids = [m.strip() for m in os.getenv("MARKET_IDS", "").split(",") if m.strip()]
    if os.getenv("LOG_LEVEL"):
        config.log_level = os.getenv("LOG_LEVEL")
    if os.getenv("PREDICT_ACCOUNT"):
        config.predict_account = os.getenv("PREDICT_ACCOUNT")
    if os.getenv("FEE_RATE_BPS"):
        config.fee_rate_bps = int(os.getenv("FEE_RATE_BPS"))
    
    return config


async def main():
    """
    Главная функция / Main function
    """
    print("=" * 60)
    print("🎯 PREDICT.FUN MARKET MAKER BOT")
    print("=" * 60)
    print()
    print("⚠️  ВАЖНО / WARNING:")
    print("   - Это НЕ финансовая рекомендация / NOT financial advice")
    print("   - Тестируйте на малых суммах / Test with small amounts")
    print("   - Читайте правила платформы / Read platform rules")
    print()
    
    # Загружаем конфигурацию / Load configuration
    config = load_config()
    
    # Получаем приватный ключ безопасно / Get private key securely
    private_key = os.getenv("PRIVATE_KEY")
    
    if not private_key:
        print("❌ PRIVATE_KEY not found in environment!")
        print()
        print("📝 Создайте файл .env с содержимым:")
        print("   PRIVATE_KEY=0x...")
        print("   PREDICT_ACCOUNT=0x... (опционально, адрес Predict Account)")
        print()
        print("   Или введите приватный ключ вручную (НЕ рекомендуется):")
        private_key = input("   Private key: ").strip()
    
    if not private_key or len(private_key) < 64:
        print("❌ Invalid private key. Exiting.")
        return
    
    # Нормализуем ключ / Normalize key
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key
    
    # Создаём и запускаем бота / Create and run bot
    bot = MarketMakerBot(config, private_key)
    await bot.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Interrupted by user. Goodbye!")
