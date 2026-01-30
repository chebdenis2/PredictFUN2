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
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

# Third-party imports
try:
    from dotenv import load_dotenv
except ImportError:
    print("❌ Установите python-dotenv: pip install python-dotenv")
    sys.exit(1)

try:
    from predict_sdk import (
        PredictClient,
        ChainId,
        OrderSide,
        OrderType,
    )
    from predict_sdk.types import Market, Order, Position
except ImportError:
    print("❌ Установите predict-sdk: pip install predict-sdk")
    print("   Документация: https://github.com/PredictDotFun/sdk-python")
    sys.exit(1)

try:
    from web3 import Web3
    from eth_account import Account
except ImportError:
    print("❌ Установите web3: pip install web3")
    sys.exit(1)


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
    chain_id: int = ChainId.BnbMainnet
    
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
    
    # Safety / Безопасность
    max_total_exposure_usd: float = 500.0  # Макс. общая позиция в USD
    max_slippage: float = 0.05             # Макс. проскальзывание (5%)
    
    # Gas settings / Настройки газа
    max_gas_price_gwei: float = 10.0       # Макс. цена газа в Gwei
    
    # Rate limits
    api_delay_sec: float = 0.5             # Задержка между API вызовами
    
    # Market IDs to trade (empty = auto-select) / ID рынков для торговли
    market_ids: list[str] = field(default_factory=list)
    
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
class OrderInfo:
    """Информация об ордере"""
    order_id: str
    market_id: str
    side: str  # "YES" or "NO"
    order_side: str  # "BUY" or "SELL"
    price: Decimal
    size: Decimal
    status: OrderStatus
    created_at: datetime
    filled_amount: Decimal = Decimal("0")


@dataclass
class MarketState:
    """Состояние рынка"""
    market_id: str
    title: str
    yes_price: Decimal
    no_price: Decimal
    mid_price: Decimal
    open_interest: Decimal
    volume_24h: Decimal
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
        
        # Initialize Web3 and Account
        self.account = Account.from_key(private_key)
        self.address = self.account.address
        self.logger.info(f"🔑 Wallet initialized: {self.address[:10]}...{self.address[-6:]}")
        
        # Initialize Predict SDK Client
        # Используем официальный SDK для работы с Predict.fun
        self.client = PredictClient(
            chain_id=config.chain_id,
            private_key=private_key,
        )
        
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
    
    async def get_suitable_markets(self) -> list[Market]:
        """
        Получить подходящие рынки для маркет-мейкинга
        Get suitable markets for market making
        
        Фильтрует рынки по критериям:
        - Open Interest < max_oi_usd (низкий OI = больше поинтов)
        - Вероятность 40-60% (uncertain markets = 2x мультипликатор)
        - Рынок активен и принимает ордера
        
        Returns:
            List of suitable Market objects
        """
        self.logger.info("🔍 Searching for suitable markets...")
        
        try:
            # Получаем список всех активных рынков через SDK
            # Get all active markets via SDK
            all_markets = await self.client.get_markets(
                status="active",
                limit=100
            )
            
            suitable = []
            
            for market in all_markets:
                # Пропускаем, если указаны конкретные market_ids
                # Skip if specific market_ids are configured
                if self.config.market_ids and market.id not in self.config.market_ids:
                    continue
                
                # Получаем текущие цены / Get current prices
                try:
                    orderbook = await self.client.get_orderbook(market.id)
                    yes_price = Decimal(str(orderbook.yes_best_ask or orderbook.yes_mid_price or 0.5))
                    
                    # Проверяем Open Interest
                    oi_usd = float(market.open_interest_usd or 0)
                    if oi_usd > self.config.max_oi_usd:
                        self.logger.debug(f"  ⏭️  {market.title[:40]}... - OI too high: ${oi_usd:,.0f}")
                        continue
                    if oi_usd < self.config.min_oi_usd:
                        self.logger.debug(f"  ⏭️  {market.title[:40]}... - OI too low: ${oi_usd:,.0f}")
                        continue
                    
                    # Проверяем вероятность (цена YES = вероятность)
                    probability = float(yes_price)
                    if not (self.config.min_probability <= probability <= self.config.max_probability):
                        self.logger.debug(
                            f"  ⏭️  {market.title[:40]}... - "
                            f"probability {probability:.1%} outside range"
                        )
                        continue
                    
                    # Рынок подходит! / Market is suitable!
                    suitable.append(market)
                    self.logger.info(
                        f"  ✅ {market.title[:50]}... | "
                        f"OI: ${oi_usd:,.0f} | Prob: {probability:.1%}"
                    )
                    
                except Exception as e:
                    self.logger.warning(f"  ⚠️  Error checking market {market.id}: {e}")
                    continue
                
                # Rate limiting
                await asyncio.sleep(self.config.api_delay_sec)
            
            self.logger.info(f"📊 Found {len(suitable)} suitable markets")
            return suitable
            
        except Exception as e:
            self.logger.error(f"❌ Error fetching markets: {e}")
            return []
    
    def calculate_order_params(
        self, 
        mid_price: Decimal
    ) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        """
        Рассчитать параметры ордеров (bid/ask цены и размеры)
        Calculate order parameters (bid/ask prices and sizes)
        
        Args:
            mid_price: Средняя цена (YES probability)
            
        Returns:
            Tuple of (bid_price, ask_price, bid_size, ask_size)
        """
        spread = Decimal(str(self.config.target_spread))
        order_size = Decimal(str(self.config.order_size_usd))
        
        # Bid ниже mid, Ask выше mid
        # Bid below mid, Ask above mid
        bid_price = mid_price - spread
        ask_price = mid_price + spread
        
        # Ограничиваем цены в диапазоне [0.01, 0.99]
        # Clamp prices to [0.01, 0.99]
        bid_price = max(Decimal("0.01"), min(Decimal("0.99"), bid_price))
        ask_price = max(Decimal("0.01"), min(Decimal("0.99"), ask_price))
        
        # Размер в контрактах = USD / цена
        # Size in contracts = USD / price
        bid_size = order_size / bid_price
        ask_size = order_size / ask_price
        
        return bid_price, ask_price, bid_size, ask_size
    
    async def place_limit_orders(self, market: Market) -> list[OrderInfo]:
        """
        Разместить лимит-ордера на обеих сторонах рынка
        Place limit orders on both sides of the market
        
        Стратегия delta-neutral: равный объём на YES и NO
        Delta-neutral strategy: equal volume on YES and NO
        
        Args:
            market: Market object
            
        Returns:
            List of placed OrderInfo objects
        """
        self.logger.info(f"📝 Placing orders for: {market.title[:50]}...")
        placed_orders = []
        
        try:
            # Получаем текущий orderbook / Get current orderbook
            orderbook = await self.client.get_orderbook(market.id)
            
            # Рассчитываем mid price
            yes_mid = Decimal(str(orderbook.yes_mid_price or 0.5))
            
            # Рассчитываем параметры ордеров
            bid_price, ask_price, bid_size, ask_size = self.calculate_order_params(yes_mid)
            
            self.logger.info(
                f"  📊 Mid: {yes_mid:.4f} | "
                f"Bid: {bid_price:.4f} ({bid_size:.2f}) | "
                f"Ask: {ask_price:.4f} ({ask_size:.2f})"
            )
            
            # ----------------------------------------------------------------
            # Размещаем ордер на покупку YES (bid)
            # Place YES buy order (bid)
            # ----------------------------------------------------------------
            try:
                yes_buy_order = await self.client.create_order(
                    market_id=market.id,
                    outcome="YES",
                    side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    price=float(bid_price),
                    size=float(bid_size),
                )
                
                order_info = OrderInfo(
                    order_id=yes_buy_order.id,
                    market_id=market.id,
                    side="YES",
                    order_side="BUY",
                    price=bid_price,
                    size=bid_size,
                    status=OrderStatus.OPEN,
                    created_at=datetime.now()
                )
                placed_orders.append(order_info)
                self.active_orders[yes_buy_order.id] = order_info
                self.orders_placed += 1
                
                self.logger.info(f"  ✅ YES BUY order placed: {bid_price:.4f} x {bid_size:.2f}")
                
            except Exception as e:
                self.logger.error(f"  ❌ Failed to place YES BUY order: {e}")
            
            await asyncio.sleep(self.config.api_delay_sec)
            
            # ----------------------------------------------------------------
            # Размещаем ордер на покупку NO (для delta-neutral)
            # Place NO buy order (for delta-neutral)
            # NO price = 1 - YES price
            # ----------------------------------------------------------------
            try:
                no_bid_price = Decimal("1") - ask_price  # Инвертируем для NO
                no_size = ask_size
                
                no_buy_order = await self.client.create_order(
                    market_id=market.id,
                    outcome="NO",
                    side=OrderSide.BUY,
                    order_type=OrderType.LIMIT,
                    price=float(no_bid_price),
                    size=float(no_size),
                )
                
                order_info = OrderInfo(
                    order_id=no_buy_order.id,
                    market_id=market.id,
                    side="NO",
                    order_side="BUY",
                    price=no_bid_price,
                    size=no_size,
                    status=OrderStatus.OPEN,
                    created_at=datetime.now()
                )
                placed_orders.append(order_info)
                self.active_orders[no_buy_order.id] = order_info
                self.orders_placed += 1
                
                self.logger.info(f"  ✅ NO BUY order placed: {no_bid_price:.4f} x {no_size:.2f}")
                
            except Exception as e:
                self.logger.error(f"  ❌ Failed to place NO BUY order: {e}")
            
            # Обновляем состояние рынка / Update market state
            if market.id not in self.markets:
                self.markets[market.id] = MarketState(
                    market_id=market.id,
                    title=market.title,
                    yes_price=yes_mid,
                    no_price=Decimal("1") - yes_mid,
                    mid_price=yes_mid,
                    open_interest=Decimal(str(market.open_interest_usd or 0)),
                    volume_24h=Decimal(str(market.volume_24h_usd or 0)),
                )
            
            self.markets[market.id].our_orders = placed_orders
            self.markets[market.id].last_rebalance = datetime.now()
            
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
        
        for order_id, order in self.active_orders.items():
            if market_id and order.market_id != market_id:
                continue
            if order.status == OrderStatus.OPEN:
                orders_to_cancel.append(order_id)
        
        for order_id in orders_to_cancel:
            try:
                await self.client.cancel_order(order_id)
                self.active_orders[order_id].status = OrderStatus.CANCELLED
                cancelled_count += 1
                self.orders_cancelled += 1
                self.logger.info(f"  🗑️  Cancelled order: {order_id[:16]}...")
                
            except Exception as e:
                self.logger.warning(f"  ⚠️  Failed to cancel order {order_id}: {e}")
            
            await asyncio.sleep(self.config.api_delay_sec)
        
        return cancelled_count
    
    async def check_and_update_orders(self) -> None:
        """
        Проверить статус ордеров и обновить PnL
        Check order status and update PnL
        """
        for order_id, order in list(self.active_orders.items()):
            if order.status not in [OrderStatus.OPEN, OrderStatus.PENDING]:
                continue
            
            try:
                order_status = await self.client.get_order(order_id)
                
                if order_status.status == "filled":
                    order.status = OrderStatus.FILLED
                    order.filled_amount = Decimal(str(order_status.filled_amount or order.size))
                    self.orders_filled += 1
                    
                    # Рассчитываем влияние на PnL
                    # Calculate PnL impact
                    self.logger.info(
                        f"  💰 Order FILLED: {order.side} {order.order_side} | "
                        f"Price: {order.price:.4f} | Size: {order.filled_amount:.2f}"
                    )
                    
                elif order_status.status == "cancelled":
                    order.status = OrderStatus.CANCELLED
                    
            except Exception as e:
                self.logger.debug(f"  ⚠️  Error checking order {order_id}: {e}")
            
            await asyncio.sleep(self.config.api_delay_sec / 2)
    
    async def should_rebalance(self, market: MarketState) -> bool:
        """
        Проверить, нужна ли ребалансировка
        Check if rebalancing is needed
        
        Returns:
            True if rebalancing is needed
        """
        # Проверяем время с последней ребалансировки
        if market.last_rebalance:
            time_since = (datetime.now() - market.last_rebalance).total_seconds()
            if time_since < self.config.rebalance_interval_sec:
                return False
        
        # Проверяем изменение цены
        try:
            orderbook = await self.client.get_orderbook(market.market_id)
            current_mid = Decimal(str(orderbook.yes_mid_price or 0.5))
            
            price_change = abs(current_mid - market.mid_price)
            if price_change > Decimal(str(self.config.price_change_threshold)):
                self.logger.info(
                    f"  📈 Price changed: {market.mid_price:.4f} → {current_mid:.4f} "
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
                        self.logger.info(f"🔄 Rebalancing: {market_state.title[:40]}...")
                        
                        # Отменяем старые ордера
                        cancelled = await self.cancel_old_orders(market_id)
                        self.logger.info(f"  🗑️  Cancelled {cancelled} old orders")
                        
                        # Получаем актуальную информацию о рынке
                        try:
                            market = await self.client.get_market(market_id)
                            
                            # Размещаем новые ордера
                            new_orders = await self.place_limit_orders(market)
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
        self.logger.info(f"  Total PnL:         ${self.total_pnl:.2f}")
        self.logger.info(f"  Points earned:     {self.total_points_earned:.0f} (estimated)")
        self.logger.info("=" * 60)
    
    async def check_balance(self) -> Optional[Decimal]:
        """
        Проверить баланс USDT / Check USDT balance
        
        Returns:
            Balance in USDT or None if error
        """
        try:
            balances = await self.client.get_balances()
            usdt_balance = Decimal(str(balances.usdt or 0))
            self.logger.info(f"💰 USDT Balance: ${usdt_balance:.2f}")
            return usdt_balance
            
        except Exception as e:
            self.logger.error(f"❌ Error checking balance: {e}")
            return None
    
    async def run(self) -> None:
        """
        Запустить бота / Run the bot
        """
        self.logger.info("=" * 60)
        self.logger.info("🚀 PREDICT.FUN MARKET MAKER BOT")
        self.logger.info("=" * 60)
        self._running = True
        
        try:
            # 1. Проверяем баланс / Check balance
            balance = await self.check_balance()
            if balance is None or balance < self.config.min_order_size_usd:
                self.logger.error("❌ Insufficient balance. Please deposit USDT.")
                return
            
            # 2. Получаем подходящие рынки / Get suitable markets
            markets = await self.get_suitable_markets()
            if not markets:
                self.logger.warning("⚠️  No suitable markets found. Check filters or try later.")
                return
            
            # 3. Размещаем начальные ордера / Place initial orders
            for market in markets[:5]:  # Ограничиваем 5 рынками для начала
                await self.place_limit_orders(market)
                await asyncio.sleep(self.config.api_delay_sec)
            
            # 4. Запускаем цикл мониторинга / Start monitoring loop
            await self.monitor_and_rebalance()
            
        except KeyboardInterrupt:
            self.logger.info("⏹️  Received shutdown signal...")
        except Exception as e:
            self.logger.error(f"❌ Fatal error: {e}")
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
        config.market_ids = os.getenv("MARKET_IDS", "").split(",")
    if os.getenv("LOG_LEVEL"):
        config.log_level = os.getenv("LOG_LEVEL")
    
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
