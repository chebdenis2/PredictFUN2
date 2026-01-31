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

# Predict.fun GraphQL API
PREDICT_GRAPHQL_URL = "https://graphql.predict.fun/graphql"

# REST API for orders (orders still go through REST)
PREDICT_API_BASE = "https://api.predict.fun"

# Wei conversion (18 decimals for most tokens)
WEI_DECIMALS = 18
WEI_MULTIPLIER = 10 ** WEI_DECIMALS


# ================================================================================
# GRAPHQL QUERIES / GRAPHQL ЗАПРОСЫ
# ================================================================================

# Query to get active categories with their markets
CATEGORIES_QUERY = """
query GetCategories($first: Int!, $status: CategoryStatus) {
  categories(filter: {status: $status}, pagination: {first: $first}) {
    edges {
      node {
        id
        title
        slug
        status
        isNegRisk
        statistics {
          liquidityValueUsd
          volume24hUsd
        }
      }
    }
  }
}
"""

# Query to get markets for a category
CATEGORY_MARKETS_QUERY = """
query GetCategoryMarkets($categoryId: ID!, $first: Int!) {
  category(id: $categoryId) {
    id
    title
    isNegRisk
    isYieldBearing
    markets(pagination: {first: $first}) {
      edges {
        node {
          id
          title
          status
          chancePercentage
          conditionId
          makerFeeBps
          takerFeeBps
          statistics {
            totalLiquidityUsd
            volume24hUsd
          }
          outcomes {
            edges {
              node {
                id
                name
                index
                onChainId
                bidPriceInCurrency
                askPriceInCurrency
              }
            }
          }
        }
      }
    }
  }
}
"""

# Query to get single market details
MARKET_QUERY = """
query GetMarket($marketId: ID!) {
  market(id: $marketId) {
    id
    title
    status
    chancePercentage
    conditionId
    makerFeeBps
    takerFeeBps
    statistics {
      totalLiquidityUsd
      volume24hUsd
    }
    outcomes {
      edges {
        node {
          id
          name
          index
          onChainId
          bidPriceInCurrency
          askPriceInCurrency
        }
      }
    }
    category {
      id
      isNegRisk
      isYieldBearing
    }
  }
}
"""


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
    min_liquidity_usd: float = 0.0       # Минимальная ликвидность ($)
    max_liquidity_usd: float = 15000.0   # Максимальная ликвидность ($) — низкая = больше поинтов
    min_probability: float = 0.40        # Минимальная вероятность (40%)
    max_probability: float = 0.60        # Максимальная вероятность (60%) — "uncertain markets" 2x
    
    # Order settings / Настройки ордеров
    target_spread: float = 0.03          # Целевой спред от mid-price (±3%)
    order_size_usd: float = 25.0         # Размер каждого ордера в USD
    min_order_size_usd: float = 5.0      # Минимальный размер ордера
    max_orders_per_market: int = 2       # Макс. ордеров на сторону (YES/NO)
    
    # Timing / Тайминги
    rebalance_interval_sec: int = 300    # Интервал ребалансировки (5 минут)
    price_change_threshold: float = 0.02 # Порог изменения цены для ребалансировки
    order_expiry_minutes: int = 60       # Время жизни ордера (минуты)
    graphql_timeout_sec: int = 10        # Таймаут GraphQL запросов
    
    # Safety / Безопасность
    max_total_exposure_usd: float = 500.0  # Макс. общая позиция в USD
    max_slippage: float = 0.05             # Макс. проскальзывание (5%)
    
    # Rate limits
    api_delay_sec: float = 0.5             # Задержка между API вызовами
    
    # Market IDs to trade (empty = auto-select) / ID рынков для торговли
    market_ids: list[str] = field(default_factory=list)
    
    # Predict Account (smart wallet address)
    predict_account: Optional[str] = None
    
    # API Authentication
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    
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
class OutcomeData:
    """Данные исхода (YES/NO)"""
    id: str
    name: str
    index: int
    on_chain_id: str
    bid_price: Optional[Decimal]
    ask_price: Optional[Decimal]


@dataclass
class MarketData:
    """Данные рынка из GraphQL"""
    market_id: str
    title: str
    status: str
    chance_percentage: float
    condition_id: str
    maker_fee_bps: int
    taker_fee_bps: int
    liquidity_usd: Decimal
    volume_24h_usd: Decimal
    outcomes: list[OutcomeData]
    is_neg_risk: bool = False
    is_yield_bearing: bool = False
    category_id: Optional[str] = None


@dataclass
class OrderInfo:
    """Информация об ордере"""
    order_id: str       # ID ордера из GraphQL (для отмены)
    order_hash: str     # Hash ордера
    market_id: str
    token_id: str
    side: Side
    price: Decimal
    size_wei: int
    status: OrderStatus
    created_at: datetime
    expires_at: datetime


@dataclass
class MarketState:
    """Состояние рынка"""
    market: MarketData
    our_orders: list[OrderInfo] = field(default_factory=list)
    last_rebalance: Optional[datetime] = None


# ================================================================================
# LOGGING SETUP / НАСТРОЙКА ЛОГИРОВАНИЯ
# ================================================================================

def setup_logging(level: str = "INFO") -> logging.Logger:
    """Настройка логирования / Setup logging"""
    logger = logging.getLogger("PredictMM")
    logger.setLevel(getattr(logging, level.upper()))
    logger.handlers.clear()
    
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.DEBUG)
    
    file_handler = logging.FileHandler(
        f"predict_mm_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )
    file_handler.setLevel(logging.DEBUG)
    
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
# GRAPHQL CLIENT / GRAPHQL КЛИЕНТ
# ================================================================================

PREDICT_REST_URL = "https://api.predict.fun"

class PredictGraphQLClient:
    """
    GraphQL клиент для Predict.fun
    
    Использует GraphQL API для получения данных о рынках и категориях.
    Использует REST API для авторизации и ордеров.
    """
    
    def __init__(
        self, 
        logger: logging.Logger,
        api_key: Optional[str] = None,
        timeout_sec: int = 10
    ):
        self.logger = logger
        self.api_key = api_key
        self.timeout_sec = timeout_sec
        self.session: Optional[aiohttp.ClientSession] = None
        self.jwt_token: Optional[str] = None  # JWT token for authenticated requests
        
        self.logger.info(f"🌐 GraphQL URL: {PREDICT_GRAPHQL_URL}")
        self.logger.info(f"🌐 REST URL: {PREDICT_REST_URL}")
    
    async def __aenter__(self):
        timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
        self.session = aiohttp.ClientSession(timeout=timeout)
        return self
    
    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()
    
    def _get_headers(self, require_auth: bool = False) -> dict:
        """Get request headers"""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Origin": "https://predict.fun",
            "Referer": "https://predict.fun/",
        }
        if self.api_key:
            headers["x-api-key"] = self.api_key  # lowercase as in working bot
        if require_auth or self.jwt_token:
            if not self.jwt_token:
                raise Exception("JWT token is required but not set")
            headers["Authorization"] = f"Bearer {self.jwt_token}"
        return headers
    
    async def execute(self, query: str, variables: dict = None) -> dict:
        """Execute GraphQL query"""
        if not self.session:
            timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
            self.session = aiohttp.ClientSession(timeout=timeout)
        
        payload = {"query": query}
        if variables:
            payload["variables"] = variables
        
        try:
            async with self.session.post(
                PREDICT_GRAPHQL_URL,
                json=payload,
                headers=self._get_headers()
            ) as response:
                data = await response.json()
                
                if "errors" in data:
                    self.logger.error(f"GraphQL Error: {data['errors']}")
                    raise Exception(f"GraphQL Error: {data['errors']}")
                
                return data.get("data", {})
                
        except asyncio.TimeoutError:
            self.logger.error(f"GraphQL timeout after {self.timeout_sec}s")
            raise
        except aiohttp.ClientError as e:
            self.logger.error(f"HTTP Error: {e}")
            raise
    
    async def get_open_categories(self, limit: int = 20) -> list[dict]:
        """Получить открытые категории / Get open categories"""
        data = await self.execute(
            CATEGORIES_QUERY,
            {"first": limit, "status": "OPEN"}
        )
        
        categories = []
        edges = data.get("categories", {}).get("edges", [])
        for edge in edges:
            node = edge.get("node", {})
            categories.append(node)
        
        return categories
    
    async def get_category_markets(self, category_id: str, limit: int = 10) -> list[MarketData]:
        """Получить рынки категории / Get category markets"""
        data = await self.execute(
            CATEGORY_MARKETS_QUERY,
            {"categoryId": category_id, "first": limit}
        )
        
        category = data.get("category", {})
        is_neg_risk = category.get("isNegRisk", False)
        is_yield_bearing = category.get("isYieldBearing", False)
        
        markets = []
        edges = category.get("markets", {}).get("edges", [])
        
        for edge in edges:
            node = edge.get("node", {})
            
            # Parse outcomes
            outcomes = []
            outcome_edges = node.get("outcomes", {}).get("edges", [])
            for oe in outcome_edges:
                on = oe.get("node", {})
                outcomes.append(OutcomeData(
                    id=on.get("id", ""),
                    name=on.get("name", ""),
                    index=on.get("index", 0),
                    on_chain_id=on.get("onChainId", ""),
                    bid_price=Decimal(str(on.get("bidPriceInCurrency") or 0)),
                    ask_price=Decimal(str(on.get("askPriceInCurrency") or 0)),
                ))
            
            stats = node.get("statistics", {})
            
            markets.append(MarketData(
                market_id=node.get("id", ""),
                title=node.get("title", ""),
                status=node.get("status", ""),
                chance_percentage=float(node.get("chancePercentage") or 50),
                condition_id=node.get("conditionId", ""),
                maker_fee_bps=int(node.get("makerFeeBps") or 0),
                taker_fee_bps=int(node.get("takerFeeBps") or 0),
                liquidity_usd=Decimal(str(stats.get("totalLiquidityUsd") or 0)),
                volume_24h_usd=Decimal(str(stats.get("volume24hUsd") or 0)),
                outcomes=outcomes,
                is_neg_risk=is_neg_risk,
                is_yield_bearing=is_yield_bearing,
                category_id=category_id,
            ))
        
        return markets
    
    async def _rest_request(self, method: str, path: str, payload: dict = None) -> dict:
        """Make REST API request"""
        if not self.session:
            timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
            self.session = aiohttp.ClientSession(timeout=timeout)
        
        url = f"{PREDICT_REST_URL}{path}"
        headers = self._get_headers(require_auth=False)
        
        try:
            if method == "GET":
                async with self.session.get(url, headers=headers) as response:
                    text = await response.text()
                    if response.status >= 400:
                        raise Exception(f"REST error {response.status}: {text[:200]}")
                    return json.loads(text) if text else {}
            else:
                async with self.session.post(url, json=payload, headers=headers) as response:
                    text = await response.text()
                    if response.status >= 400:
                        raise Exception(f"REST error {response.status}: {text[:200]}")
                    return json.loads(text) if text else {}
        except aiohttp.ClientError as e:
            raise Exception(f"REST request failed: {e}")
    
    async def _rest_request_auth(self, method: str, path: str, payload: dict = None) -> dict:
        """Make authenticated REST API request"""
        if not self.session:
            timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
            self.session = aiohttp.ClientSession(timeout=timeout)
        
        url = f"{PREDICT_REST_URL}{path}"
        headers = self._get_headers(require_auth=True)
        
        try:
            if method == "GET":
                async with self.session.get(url, headers=headers) as response:
                    text = await response.text()
                    self.logger.info(f"    REST response ({response.status}): {text[:300]}")
                    if response.status >= 400:
                        raise Exception(f"REST error {response.status}: {text[:200]}")
                    return json.loads(text) if text else {}
            else:
                async with self.session.post(url, json=payload, headers=headers) as response:
                    text = await response.text()
                    self.logger.info(f"    REST response ({response.status}): {text[:300]}")
                    if response.status >= 400:
                        raise Exception(f"REST error {response.status}: {text[:200]}")
                    return json.loads(text) if text else {}
        except aiohttp.ClientError as e:
            raise Exception(f"REST request failed: {e}")
    
    async def login_rest(self, predict_account: str, order_builder) -> bool:
        """
        Авторизация через REST API (как в рабочем боте)
        Login via REST API
        
        1. GET /v1/auth/message - получаем сообщение
        2. Подписываем через sign_predict_account_message()
        3. POST /v1/auth - получаем JWT token
        
        Args:
            predict_account: Predict Account address (smart wallet)
            order_builder: OrderBuilder instance for signing
        
        Returns:
            True if login successful
        """
        try:
            # 1. Получаем сообщение
            self.logger.info("📝 Getting auth message from REST API...")
            msg_response = await self._rest_request("GET", "/v1/auth/message")
            
            if not msg_response.get("success"):
                self.logger.error(f"Failed to get auth message: {msg_response}")
                return False
            
            message = msg_response.get("data", {}).get("message")
            if not message:
                self.logger.error(f"No message in response: {msg_response}")
                return False
            
            self.logger.info(f"📝 Got message, signing with Predict Account...")
            
            # 2. Подписываем через SDK (sign_predict_account_message)
            signature = order_builder.sign_predict_account_message(message)
            if not signature.startswith("0x"):
                signature = "0x" + signature
            
            self.logger.info(f"✍️ Signature: {signature[:20]}...")
            
            # 3. POST /v1/auth
            auth_payload = {
                "signer": predict_account,  # Predict Account address, not Privy wallet!
                "message": message,
                "signature": signature,
            }
            
            auth_response = await self._rest_request("POST", "/v1/auth", auth_payload)
            
            if not auth_response.get("success"):
                self.logger.error(f"Auth failed: {auth_response}")
                return False
            
            token = auth_response.get("data", {}).get("token")
            if token:
                self.jwt_token = token
                self.logger.info(f"🔐 Logged in successfully via REST!")
                return True
            else:
                self.logger.error(f"No token in auth response: {auth_response}")
                return False
                
        except Exception as e:
            self.logger.error(f"Login error: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return False
    
    async def create_order_rest(self, order_payload: dict) -> dict:
        """
        Создать ордер через REST API (как в рабочем боте)
        Create order via REST API
        
        Args:
            order_payload: Full payload with "data" key containing order info
        
        Returns:
            Order response dict
        """
        if not self.jwt_token:
            self.logger.error("    No JWT token! Please login first.")
            raise Exception("Not authenticated")
        
        self.logger.info(f"    Submitting order to REST API /v1/orders...")
        self.logger.info(f"    JWT token: {self.jwt_token[:30]}...")
        
        try:
            response = await self._rest_request_auth("POST", "/v1/orders", order_payload)
            
            if response.get("success"):
                order_data = response.get("data", {})
                order_id = order_data.get("orderId", order_data.get("id", "unknown"))
                self.logger.info(f"    ✅ Order created: {order_id}")
                # Normalize the response to have 'id' key
                order_data["id"] = order_id
                return order_data
            else:
                self.logger.error(f"    ❌ Order failed: {response}")
                raise Exception(f"Order failed: {response.get('message', 'Unknown error')}")
                
        except Exception as e:
            self.logger.error(f"    REST order error: {e}")
            raise
    
    async def cancel_orders_rest(self, order_ids: list[str]) -> bool:
        """
        Отменить ордера через REST API
        Cancel orders via REST API
        
        Args:
            order_ids: List of order IDs (numeric, not hashes!) to cancel
        """
        if not order_ids:
            return True
        
        # Filter out empty strings and hashes (API expects numeric IDs)
        valid_ids = [oid for oid in order_ids if oid and not oid.startswith("0x")]
        if not valid_ids:
            self.logger.warning(f"    No valid order IDs to cancel (got: {order_ids})")
            return False
            
        try:
            self.logger.info(f"    Cancelling orders: {valid_ids}")
            payload = {"data": {"ids": valid_ids}}
            response = await self._rest_request_auth("POST", "/v1/orders/remove", payload)
            return response.get("success", False)
        except Exception as e:
            self.logger.warning(f"Cancel orders error: {e}")
            return False
    
    async def cancel_order(self, order_id: str) -> bool:
        """Cancel single order via REST API"""
        return await self.cancel_orders_rest([order_id])
    
    async def get_market(self, market_id: str) -> Optional[MarketData]:
        """Получить данные одного рынка / Get single market data"""
        try:
            data = await self.execute(MARKET_QUERY, {"marketId": market_id})
            node = data.get("market", {})
            
            if not node:
                return None
            
            # Parse outcomes
            outcomes = []
            outcome_edges = node.get("outcomes", {}).get("edges", [])
            for oe in outcome_edges:
                on = oe.get("node", {})
                outcomes.append(OutcomeData(
                    id=on.get("id", ""),
                    name=on.get("name", ""),
                    index=on.get("index", 0),
                    on_chain_id=on.get("onChainId", ""),
                    bid_price=Decimal(str(on.get("bidPriceInCurrency") or 0)),
                    ask_price=Decimal(str(on.get("askPriceInCurrency") or 0)),
                ))
            
            stats = node.get("statistics", {})
            category = node.get("category", {})
            
            return MarketData(
                market_id=node.get("id", ""),
                title=node.get("title", ""),
                status=node.get("status", ""),
                chance_percentage=float(node.get("chancePercentage") or 50),
                condition_id=node.get("conditionId", ""),
                maker_fee_bps=int(node.get("makerFeeBps") or 0),
                taker_fee_bps=int(node.get("takerFeeBps") or 0),
                liquidity_usd=Decimal(str(stats.get("totalLiquidityUsd") or 0)),
                volume_24h_usd=Decimal(str(stats.get("volume24hUsd") or 0)),
                outcomes=outcomes,
                is_neg_risk=category.get("isNegRisk", False),
                is_yield_bearing=category.get("isYieldBearing", False),
                category_id=category.get("id"),
            )
            
        except Exception as e:
            self.logger.error(f"Error fetching market {market_id}: {e}")
            return None


# ================================================================================
# GraphQL ORDER MUTATIONS
# ================================================================================

CREATE_ORDER_MUTATION = """
mutation CreateOrder($data: CreateOrderInput!) {
  createOrder(data: $data) {
    order {
      id
      hash
      status
    }
    code
  }
}
"""

CANCEL_ORDER_MUTATION = """
mutation CancelOrder($data: CancelOrderInput!) {
  cancelOrder(data: $data)
}
"""

LOGIN_MUTATION = """
mutation Login($data: AccountLoginInput!) {
  login(data: $data) {
    auth {
      address
      token
    }
  }
}
"""

GET_LOGIN_MESSAGE_QUERY = """
query GetLoginMessage($timestamp: Timestamp!) {
  message(timestamp: $timestamp)
}
"""


# ================================================================================
# MARKET MAKER BOT / MARKET MAKING БОТ
# ================================================================================

class MarketMakerBot:
    """
    Market Making Bot для Predict.fun
    
    Использует GraphQL API для получения данных о рынках
    и SDK для построения и подписания ордеров.
    """
    
    def __init__(self, config: BotConfig, private_key: str):
        """
        Инициализация бота / Initialize bot
        """
        self.config = config
        self.logger = setup_logging(config.log_level)
        
        # Initialize Account
        self.account = Account.from_key(private_key)
        self.address = self.account.address
        self.logger.info(f"🔑 Wallet: {self.address[:10]}...{self.address[-6:]}")
        
        self.predict_account = config.predict_account
        if self.predict_account:
            self.logger.info(f"📱 Predict Account: {self.predict_account[:10]}...{self.predict_account[-6:]}")
        
        # Initialize SDK OrderBuilder
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
        
        # API Client (GraphQL for everything)
        effective_key = config.api_key or config.api_secret
        self.graphql_client = PredictGraphQLClient(
            logger=self.logger,
            api_key=effective_key,
            timeout_sec=config.graphql_timeout_sec
        )
        
        # Store private key for login
        self._private_key = private_key
        
        if effective_key:
            self.logger.info(f"🔐 API Key: {effective_key[:8]}...{effective_key[-4:]}")
        
        # State
        self.markets: dict[str, MarketState] = {}
        self.active_orders: dict[str, OrderInfo] = {}
        
        # Statistics
        self.orders_placed: int = 0
        self.orders_filled: int = 0
        self.orders_cancelled: int = 0
        
        self._running: bool = False
        
        self.logger.info("✅ MarketMakerBot initialized")
    
    async def get_suitable_markets(self) -> list[MarketData]:
        """
        Получить подходящие рынки для маркет-мейкинга
        """
        self.logger.info("🔍 Searching for suitable markets...")
        
        suitable = []
        
        try:
            # Получаем открытые категории
            categories = await self.graphql_client.get_open_categories(limit=30)
            self.logger.info(f"📂 Found {len(categories)} open categories")
            
            for cat in categories:
                cat_id = cat.get("id", "")
                cat_title = cat.get("title", "")[:40]
                
                # Получаем рынки категории
                try:
                    markets = await self.graphql_client.get_category_markets(cat_id, limit=10)
                    
                    for market in markets:
                        # Фильтруем по конфигурации
                        if self._is_market_suitable(market):
                            suitable.append(market)
                            self.logger.info(
                                f"  ✅ {market.title[:50]}... | "
                                f"Prob: {market.chance_percentage:.0f}% | "
                                f"Liq: ${float(market.liquidity_usd):,.0f}"
                            )
                    
                except Exception as e:
                    self.logger.debug(f"  ⚠️  Error fetching markets for {cat_title}: {e}")
                
                await asyncio.sleep(self.config.api_delay_sec)
            
            self.logger.info(f"📊 Found {len(suitable)} suitable markets")
            return suitable
            
        except Exception as e:
            self.logger.error(f"❌ Error fetching markets: {e}")
            return []
    
    def _is_market_suitable(self, market: MarketData) -> bool:
        """Проверить подходит ли рынок"""
        # Проверяем статус
        if market.status not in ["CREATED", "REGISTERED", "UNPAUSED"]:
            return False
        
        # Проверяем вероятность
        prob = market.chance_percentage / 100.0
        if not (self.config.min_probability <= prob <= self.config.max_probability):
            return False
        
        # Проверяем ликвидность
        liq = float(market.liquidity_usd)
        if liq > self.config.max_liquidity_usd:
            return False
        if liq < self.config.min_liquidity_usd:
            return False
        
        # Проверяем outcomes
        if len(market.outcomes) < 2:
            return False
        
        # Проверяем наличие on_chain_id
        for outcome in market.outcomes:
            if not outcome.on_chain_id:
                return False
        
        return True
    
    def calculate_order_params(
        self, 
        mid_price: Decimal
    ) -> tuple[Decimal, Decimal, int, int]:
        """Рассчитать параметры ордеров"""
        spread = Decimal(str(self.config.target_spread))
        order_size = Decimal(str(self.config.order_size_usd))
        
        bid_price = max(Decimal("0.01"), min(Decimal("0.99"), mid_price - spread))
        ask_price = max(Decimal("0.01"), min(Decimal("0.99"), mid_price + spread))
        
        bid_size_wei = int((order_size / bid_price) * WEI_MULTIPLIER)
        ask_size_wei = int((order_size / ask_price) * WEI_MULTIPLIER)
        
        return bid_price, ask_price, bid_size_wei, ask_size_wei
    
    def price_to_wei(self, price: Decimal) -> int:
        """Convert price to wei"""
        return int(price * WEI_MULTIPLIER)
    
    def _quantity_step(self, price_per_share_wei: int) -> int:
        """Calculate quantity step for precision (from working bot)"""
        import math
        base = 10**13
        if price_per_share_wei <= 0:
            return base
        price_units = price_per_share_wei // base
        if price_units <= 0:
            return base
        step_multiplier = 100000 // math.gcd(price_units, 100000)
        return base * step_multiplier
    
    def _quantize_quantity_wei(self, quantity_wei: int, price_per_share_wei: int) -> int:
        """Quantize quantity to required precision (from working bot)"""
        if quantity_wei <= 0:
            return 0
        step = self._quantity_step(price_per_share_wei)
        if step <= 0:
            return quantity_wei
        return (quantity_wei // step) * step
    
    def _amounts_ok(self, amounts) -> bool:
        """Check if amounts have valid precision"""
        maker = int(getattr(amounts, "maker_amount", 0))
        taker = int(getattr(amounts, "taker_amount", 0))
        return maker % (10**13) == 0 and taker % (10**13) == 0
    
    def _get_valid_amounts(self, side: Side, price_per_share_wei: int, quantity_wei: int):
        """Get amounts with valid precision, adjusting quantity if needed"""
        step = self._quantity_step(price_per_share_wei)
        attempts = 0
        
        while quantity_wei > 0:
            # Quantize quantity first
            quantized_qty = self._quantize_quantity_wei(quantity_wei, price_per_share_wei)
            if quantized_qty <= 0:
                break
            
            limit_input = LimitHelperInput(
                side=side,
                price_per_share_wei=price_per_share_wei,
                quantity_wei=quantized_qty
            )
            
            amounts = self.order_builder.get_limit_order_amounts(limit_input)
            
            if self._amounts_ok(amounts):
                return amounts, quantized_qty
            
            # Try with smaller quantity
            quantity_wei -= step
            attempts += 1
            if attempts >= 10:
                break
        
        return None, 0
    
    async def build_and_sign_order(
        self,
        market: MarketData,
        outcome: OutcomeData,
        side: Side,
        price: Decimal,
        quantity_wei: int,
    ) -> Optional[dict]:
        """
        Построить и подписать ордер, вернуть payload для REST API
        
        Format (как в рабочем боте):
        {
            "data": {
                "order": {...signed order fields...},
                "pricePerShare": str(price_per_share_wei),
                "strategy": "LIMIT"
            }
        }
        """
        try:
            price_wei = self.price_to_wei(price)
            
            # Get amounts with valid precision (quantized)
            amounts, quantized_qty = self._get_valid_amounts(side, price_wei, quantity_wei)
            
            if not amounts or quantized_qty <= 0:
                self.logger.error(f"    Failed to get valid amounts for qty={quantity_wei}, price={price_wei}")
                return None
            
            expires_at = datetime.now() + timedelta(minutes=self.config.order_expiry_minutes)
            
            # Fee rate must be at least 200 bps (2%) per API requirement
            fee_rate = max(market.taker_fee_bps, market.maker_fee_bps, 200)
            self.logger.info(f"    Fee rate: {fee_rate} bps (market: maker={market.maker_fee_bps}, taker={market.taker_fee_bps})")
            
            order_input = BuildOrderInput(
                side=side,
                token_id=outcome.on_chain_id,
                maker_amount=str(amounts.maker_amount),
                taker_amount=str(amounts.taker_amount),
                fee_rate_bps=str(fee_rate),
                expires_at=expires_at
            )
            
            # 1. Build order
            order = self.order_builder.build_order(strategy="LIMIT", data=order_input)
            self.logger.info(f"    Step 1 OK: Order built")
            
            # 2. Build typed data for signing
            # Log signature params for debugging
            self.logger.info(f"    Signature params: is_neg_risk={market.is_neg_risk}, is_yield_bearing={market.is_yield_bearing}")
            
            typed_data = self.order_builder.build_typed_data(
                order,
                is_neg_risk=market.is_neg_risk,
                is_yield_bearing=market.is_yield_bearing
            )
            self.logger.info(f"    Step 2 OK: TypedData built")
            
            # 3. Sign the typed data
            signed_order = self.order_builder.sign_typed_data_order(typed_data)
            self.logger.info(f"    Step 3 OK: Order signed")
            
            # 4. Build hash
            order_hash = self.order_builder.build_typed_data_hash(typed_data)
            
            # REST API payload format (как в рабочем боте)
            order_payload = {
                "hash": order_hash,
                "salt": str(signed_order.salt),
                "maker": signed_order.maker,
                "signer": signed_order.signer,
                "taker": signed_order.taker,
                "tokenId": str(signed_order.token_id),
                "makerAmount": str(signed_order.maker_amount),
                "takerAmount": str(signed_order.taker_amount),
                "expiration": str(signed_order.expiration),
                "nonce": str(signed_order.nonce),
                "feeRateBps": str(signed_order.fee_rate_bps),
                "side": int(signed_order.side.value if hasattr(signed_order.side, 'value') else signed_order.side),
                "signatureType": int(signed_order.signature_type.value if hasattr(signed_order.signature_type, 'value') else signed_order.signature_type),
                "signature": signed_order.signature,
            }
            
            return {
                "data": {
                    "order": order_payload,
                    "pricePerShare": str(amounts.price_per_share),
                    "strategy": "LIMIT",
                }
            }
            
        except Exception as e:
            self.logger.error(f"❌ Error building order: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return None
    
    async def place_limit_orders(self, market: MarketData) -> list[OrderInfo]:
        """Разместить лимит-ордера на рынке"""
        self.logger.info(f"📝 Placing orders: {market.title[:50]}...")
        placed_orders = []
        
        try:
            # Находим outcomes по индексу (index 0 и 1)
            # Для бинарных рынков: index 0 = первый исход, index 1 = второй исход
            # Для YES/NO рынков: обычно YES=0, NO=1
            outcomes_by_index = {o.index: o for o in market.outcomes}
            
            outcome_0 = outcomes_by_index.get(0)
            outcome_1 = outcomes_by_index.get(1)
            
            if not outcome_0 or not outcome_1:
                # Попробуем просто взять первые два outcome
                if len(market.outcomes) >= 2:
                    outcome_0 = market.outcomes[0]
                    outcome_1 = market.outcomes[1]
                else:
                    self.logger.warning(f"  ⚠️  Need at least 2 outcomes, got {len(market.outcomes)}")
                    return []
            
            self.logger.info(f"  🎯 Outcomes: [{outcome_0.name}] vs [{outcome_1.name}]")
            
            # Рассчитываем mid price из chancePercentage (вероятность первого исхода)
            mid_price = Decimal(str(market.chance_percentage / 100.0))
            bid_price, ask_price, bid_size_wei, ask_size_wei = self.calculate_order_params(mid_price)
            
            # Delta-neutral цена для второго исхода
            outcome_1_price = max(Decimal("0.01"), min(Decimal("0.99"), Decimal("1") - ask_price))
            
            self.logger.info(f"  📊 DELTA-NEUTRAL STRATEGY:")
            self.logger.info(f"     Mid price: {mid_price:.4f} (from probability {market.chance_percentage:.0f}%)")
            self.logger.info(f"     Spread: {self.config.target_spread:.2%}")
            self.logger.info(f"     {outcome_0.name}: BUY @ {bid_price:.4f} (mid - spread)")
            self.logger.info(f"     {outcome_1.name}: BUY @ {outcome_1_price:.4f} (1 - ask = 1 - {ask_price:.4f})")
            self.logger.info(f"     Order size: ${self.config.order_size_usd:.2f} each side")
            
            # Размещаем ордер на первый исход (outcome_0)
            if outcome_0.on_chain_id:
                order = await self._place_single_order(
                    market, outcome_0, Side.BUY, bid_price, bid_size_wei
                )
                if order:
                    placed_orders.append(order)
            else:
                self.logger.warning(f"  ⚠️  {outcome_0.name} has no on_chain_id")
            
            await asyncio.sleep(self.config.api_delay_sec)
            
            # Размещаем ордер на второй исход (outcome_1) для delta-neutral
            if outcome_1.on_chain_id:
                order = await self._place_single_order(
                    market, outcome_1, Side.BUY, outcome_1_price, ask_size_wei
                )
                if order:
                    placed_orders.append(order)
            else:
                self.logger.warning(f"  ⚠️  {outcome_1.name} has no on_chain_id")
            
            # Обновляем состояние
            if market.market_id not in self.markets:
                self.markets[market.market_id] = MarketState(market=market)
            
            self.markets[market.market_id].our_orders = placed_orders
            self.markets[market.market_id].last_rebalance = datetime.now()
            
            return placed_orders
            
        except Exception as e:
            self.logger.error(f"❌ Error placing orders: {e}")
            return []
    
    async def _place_single_order(
        self,
        market: MarketData,
        outcome: OutcomeData,
        side: Side,
        price: Decimal,
        size_wei: int
    ) -> Optional[OrderInfo]:
        """Разместить один ордер через REST API"""
        try:
            order_payload = await self.build_and_sign_order(
                market=market,
                outcome=outcome,
                side=side,
                price=price,
                quantity_wei=size_wei,
            )
            
            if not order_payload:
                return None
            
            # Извлекаем hash из payload для логирования
            order_hash = order_payload.get("data", {}).get("order", {}).get("hash", "")
            
            # Отправляем через REST API
            result = await self.graphql_client.create_order_rest(order_payload)
            
            order_id = result.get("id", "") if result else ""
            result_hash = result.get("orderHash", result.get("hash", order_hash)) if result else order_hash
            
            order_info = OrderInfo(
                order_id=order_id,
                order_hash=result_hash,
                market_id=market.market_id,
                token_id=outcome.on_chain_id,
                side=side,
                price=price,
                size_wei=size_wei,
                status=OrderStatus.OPEN,
                created_at=datetime.now(),
                expires_at=datetime.now() + timedelta(minutes=self.config.order_expiry_minutes)
            )
            
            # Используем order_id как ключ
            key = order_info.order_id or order_info.order_hash
            self.active_orders[key] = order_info
            self.orders_placed += 1
            
            self.logger.info(f"  ✅ {outcome.name} {side.name} @ {price:.4f}")
            return order_info
            
        except Exception as e:
            self.logger.error(f"  ❌ Failed {outcome.name} order: {e}")
            return None
    
    async def cancel_old_orders(self, market_id: Optional[str] = None) -> int:
        """Отменить старые ордера через GraphQL"""
        cancelled = 0
        
        for key, order in list(self.active_orders.items()):
            if market_id and order.market_id != market_id:
                continue
            if order.status != OrderStatus.OPEN:
                continue
            
            try:
                # Используем order_id для отмены
                cancel_id = order.order_id or order.order_hash
                if cancel_id:
                    success = await self.graphql_client.cancel_order(cancel_id)
                    if success:
                        order.status = OrderStatus.CANCELLED
                        cancelled += 1
                        self.orders_cancelled += 1
                        self.logger.info(f"  🗑️  Cancelled: {cancel_id[:16]}...")
            except Exception as e:
                self.logger.warning(f"  ⚠️  Cancel failed: {e}")
            
            await asyncio.sleep(self.config.api_delay_sec)
        
        return cancelled
    
    async def _rebalance_existing_markets(self) -> None:
        """Ребалансировка существующих рынков"""
        for market_id, state in list(self.markets.items()):
            try:
                # Проверяем нужна ли ребалансировка
                if state.last_rebalance:
                    elapsed = (datetime.now() - state.last_rebalance).total_seconds()
                    if elapsed < self.config.rebalance_interval_sec:
                        continue
                
                self.logger.info(f"🔄 Rebalancing: {state.market.title[:40]}...")
                
                # Отменяем старые
                await self.cancel_old_orders(market_id)
                
                # Получаем актуальные данные
                updated = await self.graphql_client.get_market(market_id)
                if updated and self._is_market_suitable(updated):
                    state.market = updated
                    await self.place_limit_orders(updated)
                else:
                    # Рынок больше не подходит - удаляем
                    self.logger.info(f"  📤 Market no longer suitable, removing...")
                    del self.markets[market_id]
                
                await asyncio.sleep(self.config.api_delay_sec)
                
            except Exception as e:
                self.logger.error(f"❌ Rebalance error for {market_id}: {e}")
    
    def log_statistics(self) -> None:
        """Логирование статистики"""
        self.logger.info("=" * 60)
        self.logger.info("📊 STATISTICS")
        self.logger.info(f"  Markets: {len(self.markets)} | Orders placed: {self.orders_placed}")
        self.logger.info(f"  Filled: {self.orders_filled} | Cancelled: {self.orders_cancelled}")
        active = sum(1 for o in self.active_orders.values() if o.status == OrderStatus.OPEN)
        self.logger.info(f"  Active orders: {active}")
        self.logger.info("=" * 60)
    
    async def run(self) -> None:
        """Запустить бота"""
        self.logger.info("=" * 60)
        self.logger.info("🚀 PREDICT.FUN MARKET MAKER BOT")
        self.logger.info("=" * 60)
        self._running = True
        
        async with self.graphql_client:
            try:
                # Авторизуемся через REST API с Predict Account
                # signer = Predict Account address (smart wallet)
                # signature = sign_predict_account_message() от SDK
                logged_in = await self.graphql_client.login_rest(
                    predict_account=self.predict_account,
                    order_builder=self.order_builder
                )
                if not logged_in:
                    self.logger.error("❌ Failed to login. Cannot place orders.")
                    return
                
                # Главный цикл бота
                while True:
                    try:
                        # Получаем рынки
                        markets = await self.get_suitable_markets()
                        
                        if not markets:
                            self.logger.warning("⚠️  No suitable markets found. Waiting...")
                            await asyncio.sleep(self.config.rebalance_interval_sec)
                            continue
                        
                        # Размещаем ордера на новых рынках
                        for market in markets[:5]:
                            if market.market_id not in self.markets:
                                await self.place_limit_orders(market)
                                await asyncio.sleep(self.config.api_delay_sec)
                        
                        # Ребалансировка существующих позиций
                        await self._rebalance_existing_markets()
                        
                        self.log_statistics()
                        await asyncio.sleep(self.config.rebalance_interval_sec)
                        
                    except asyncio.CancelledError:
                        break
                    except Exception as e:
                        self.logger.error(f"❌ Main loop error: {e}")
                        await asyncio.sleep(30)
                
            except KeyboardInterrupt:
                self.logger.info("⏹️  Shutdown signal...")
            except Exception as e:
                self.logger.error(f"❌ Fatal: {e}")
                import traceback
                traceback.print_exc()
            finally:
                await self.shutdown()
    
    async def shutdown(self) -> None:
        """Graceful shutdown"""
        self.logger.info("🛑 Shutting down...")
        self._running = False
        await self.cancel_old_orders()
        self.log_statistics()
        self.logger.info("👋 Goodbye!")


# ================================================================================
# MAIN / ТОЧКА ВХОДА
# ================================================================================

def load_config() -> BotConfig:
    """Загрузить конфигурацию"""
    load_dotenv()
    
    config = BotConfig()
    
    if os.getenv("MIN_LIQUIDITY_USD"):
        config.min_liquidity_usd = float(os.getenv("MIN_LIQUIDITY_USD"))
    if os.getenv("MAX_LIQUIDITY_USD"):
        config.max_liquidity_usd = float(os.getenv("MAX_LIQUIDITY_USD"))
    if os.getenv("TARGET_SPREAD"):
        config.target_spread = float(os.getenv("TARGET_SPREAD"))
    if os.getenv("ORDER_SIZE_USD"):
        config.order_size_usd = float(os.getenv("ORDER_SIZE_USD"))
    if os.getenv("REBALANCE_INTERVAL_SEC"):
        config.rebalance_interval_sec = int(os.getenv("REBALANCE_INTERVAL_SEC"))
    if os.getenv("GRAPHQL_TIMEOUT_SEC"):
        config.graphql_timeout_sec = int(os.getenv("GRAPHQL_TIMEOUT_SEC"))
    if os.getenv("MARKET_IDS"):
        config.market_ids = [m.strip() for m in os.getenv("MARKET_IDS", "").split(",") if m.strip()]
    if os.getenv("LOG_LEVEL"):
        config.log_level = os.getenv("LOG_LEVEL")
    if os.getenv("PREDICT_ACCOUNT"):
        config.predict_account = os.getenv("PREDICT_ACCOUNT")
    if os.getenv("API_KEY"):
        config.api_key = os.getenv("API_KEY")
    if os.getenv("API_SECRET") or os.getenv("API_SECRET_KEY"):
        config.api_secret = os.getenv("API_SECRET") or os.getenv("API_SECRET_KEY")
    if os.getenv("MIN_PROB_PERCENT"):
        config.min_probability = float(os.getenv("MIN_PROB_PERCENT")) / 100.0
    if os.getenv("MAX_PROB_PERCENT"):
        config.max_probability = float(os.getenv("MAX_PROB_PERCENT")) / 100.0
    if os.getenv("ORDER_EXPIRY_MINUTES"):
        config.order_expiry_minutes = int(os.getenv("ORDER_EXPIRY_MINUTES"))
    
    return config


async def main():
    """Main function"""
    print("=" * 60)
    print("🎯 PREDICT.FUN MARKET MAKER BOT (GraphQL)")
    print("=" * 60)
    print()
    print("⚠️  NOT FINANCIAL ADVICE - TEST WITH SMALL AMOUNTS")
    print()
    
    config = load_config()
    
    private_key = os.getenv("PRIVATE_KEY")
    if not private_key:
        print("❌ PRIVATE_KEY not found!")
        print("   Add to .env: PRIVATE_KEY=0x...")
        private_key = input("   Enter private key: ").strip()
    
    if not private_key or len(private_key) < 64:
        print("❌ Invalid private key")
        return
    
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key
    
    bot = MarketMakerBot(config, private_key)
    await bot.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Interrupted")
