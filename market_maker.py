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
    endsAt
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
    
    # Position limits / Лимиты позиций
    max_position_usd: float = 50.0       # Максимум $ на одну позицию
    
    # Market order fallback / Рыночный ордер для delta-neutral
    enable_market_fallback: bool = True  # Разрешить рыночные ордера для delta-neutral
    max_market_loss_percent: float = 2.0 # Макс. убыток % для рыночного входа (если больше — оставить лимитку)
    
    # Market rotation / Ротация рынков
    max_market_hold_minutes: int = 60    # Максимум минут на одном рынке (0 = без лимита)
    min_time_to_expiry_minutes: int = 30 # Не входить если до экспирации < X минут
    
    # Position recovery / Восстановление позиций
    recover_positions_on_start: bool = True  # Восстанавливать позиции при старте
    
    # Order settings / Настройки ордеров
    target_spread: float = 0.015         # Целевой спред от mid-price (±1.5% - ближе к рынку!)
    order_size_usd: float = 25.0         # Размер каждого ордера в USD
    min_order_size_usd: float = 5.0      # Минимальный размер ордера
    max_orders_per_market: int = 2       # Макс. ордеров на сторону (YES/NO)
    aggressive_pricing: bool = False     # True = ставить ордера ближе к рынку (0.5% spread)
    
    # Position growth control / Контроль роста позиции
    stop_new_orders_after_fill: bool = True  # НЕ выставлять новые лимитки если уже есть позиция
                                              # (только хеджировать, не наращивать позицию)
    
    # Timing / Тайминги
    rebalance_interval_sec: int = 1200   # Интервал ребалансировки (20 минут - больше времени для fill!)
    price_change_threshold: float = 0.02 # Порог изменения цены для ребалансировки (2%)
    skip_rebalance_if_price_stable: bool = True  # Не ребалансировать если цена не изменилась
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
    ends_at: Optional[datetime] = None  # Время экспирации рынка


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
    entered_at: Optional[datetime] = None  # Когда вошли в рынок (для ротации)
    # Position tracking
    position_usd: float = 0.0  # Текущий размер позиции в USD
    filled_outcome_0: bool = False  # Сработал ли ордер на первый исход
    filled_outcome_1: bool = False  # Сработал ли ордер на второй исход
    has_unhedged_position: bool = False  # Есть незахеджированная позиция (только hedge ордера)
    # Price tracking for smart rebalancing
    last_mid_price: Optional[Decimal] = None  # Последняя mid-price при размещении ордеров


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
    
    # JWT токен живёт ~4 часа, обновляем каждые 3 часа для надёжности
    JWT_REFRESH_INTERVAL_SEC = 3 * 60 * 60  # 3 hours
    
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
        
        # Для автоматического обновления JWT
        self._jwt_created_at: Optional[datetime] = None
        self._predict_account: Optional[str] = None
        self._order_builder = None  # OrderBuilder для повторной авторизации
        self._reauth_in_progress: bool = False  # Флаг для избежания рекурсии
        
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
        
        # Parse ends_at
        ends_at = None
        ends_at_str = category.get("endsAt")
        if ends_at_str:
            try:
                # ISO format: 2026-01-30T15:00:00.000Z
                ends_at = datetime.fromisoformat(ends_at_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass
        
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
                ends_at=ends_at,
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
    
    async def _rest_request_auth(self, method: str, path: str, payload: dict = None, _retry: bool = True) -> dict:
        """Make authenticated REST API request with auto-refresh JWT"""
        if not self.session:
            timeout = aiohttp.ClientTimeout(total=self.timeout_sec)
            self.session = aiohttp.ClientSession(timeout=timeout)
        
        # Проверяем и обновляем JWT если нужно
        await self._ensure_jwt_valid()
        
        url = f"{PREDICT_REST_URL}{path}"
        headers = self._get_headers(require_auth=True)
        
        try:
            if method == "GET":
                async with self.session.get(url, headers=headers) as response:
                    text = await response.text()
                    self.logger.info(f"    REST response ({response.status}): {text[:300]}")
                    
                    # При 401 пытаемся переавторизоваться
                    if response.status == 401 and _retry:
                        self.logger.warning("    ⚠️ JWT expired (401), re-authenticating...")
                        if await self._reauth():
                            return await self._rest_request_auth(method, path, payload, _retry=False)
                    
                    if response.status >= 400:
                        raise Exception(f"REST error {response.status}: {text[:200]}")
                    return json.loads(text) if text else {}
            else:
                async with self.session.post(url, json=payload, headers=headers) as response:
                    text = await response.text()
                    self.logger.info(f"    REST response ({response.status}): {text[:300]}")
                    
                    # При 401 пытаемся переавторизоваться
                    if response.status == 401 and _retry:
                        self.logger.warning("    ⚠️ JWT expired (401), re-authenticating...")
                        if await self._reauth():
                            return await self._rest_request_auth(method, path, payload, _retry=False)
                    
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
                self._jwt_created_at = datetime.now()
                # Сохраняем параметры для повторной авторизации
                self._predict_account = predict_account
                self._order_builder = order_builder
                self.logger.info(f"🔐 Logged in successfully via REST! (token valid for ~4h)")
                return True
            else:
                self.logger.error(f"No token in auth response: {auth_response}")
                return False
                
        except Exception as e:
            self.logger.error(f"Login error: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
            return False
    
    async def _ensure_jwt_valid(self) -> bool:
        """
        Проверить и обновить JWT токен если он скоро истечёт
        
        Returns:
            True если токен валиден (или успешно обновлён)
        """
        if self._reauth_in_progress:
            return bool(self.jwt_token)
        
        # Если нет токена или времени создания - нужна авторизация
        if not self.jwt_token or not self._jwt_created_at:
            if self._predict_account and self._order_builder:
                self.logger.info("🔄 JWT token missing, re-authenticating...")
                return await self._reauth()
            return False
        
        # Проверяем возраст токена
        age_sec = (datetime.now() - self._jwt_created_at).total_seconds()
        
        # Если токен старше порога - обновляем
        if age_sec >= self.JWT_REFRESH_INTERVAL_SEC:
            self.logger.info(f"🔄 JWT token age {age_sec/3600:.1f}h >= {self.JWT_REFRESH_INTERVAL_SEC/3600:.1f}h, refreshing...")
            return await self._reauth()
        
        return True
    
    async def _reauth(self) -> bool:
        """Повторная авторизация"""
        if self._reauth_in_progress:
            return bool(self.jwt_token)
        
        if not self._predict_account or not self._order_builder:
            self.logger.error("Cannot re-authenticate: missing credentials")
            return False
        
        self._reauth_in_progress = True
        try:
            self.logger.info("🔐 Re-authenticating...")
            result = await self.login_rest(self._predict_account, self._order_builder)
            if result:
                self.logger.info("✅ Re-authentication successful!")
            else:
                self.logger.error("❌ Re-authentication failed!")
            return result
        finally:
            self._reauth_in_progress = False
    
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
    
    async def get_positions(self) -> Optional[list[dict]]:
        """
        Получить открытые позиции через REST API
        GET /v1/positions
        
        Returns:
            list[dict] - список позиций
            None - при ошибке API (чтобы различать "нет позиций" от "ошибка")
        
        Response format:
        {
            "success": true,
            "data": [
                {
                    "id": "base64...",
                    "valueUsd": "18.76",
                    "market": {"id": "6126", "title": "$85,000..."},
                    "outcome": {"id": "...", "name": "YES", "onChainId": "12345..."},
                    "shares": "50.7",
                    "avgPrice": "39.4" (in cents)
                }
            ]
        }
        """
        try:
            response = await self._rest_request_auth("GET", "/v1/positions")
            if response.get("success"):
                return response.get("data", [])
            self.logger.warning(f"get_positions: API returned success=false")
            return None  # API ошибка, не пустой список
        except Exception as e:
            self.logger.warning(f"Failed to get positions: {e}")
            return None  # При ошибке возвращаем None, а не пустой список
    
    async def get_open_orders(self, status: str = "OPEN") -> list[dict]:
        """
        Получить ордера через REST API
        GET /v1/orders?status=OPEN
        
        Response format:
        {
            "success": true,
            "data": [
                {
                    "order": {
                        "hash": "0x...",
                        "tokenId": "12345...",
                        "maker": "0x...",
                        ...
                    },
                    "id": "4290750",
                    "marketId": "6169",
                    "pricePerShare": "540000000000000000",
                    ...
                }
            ]
        }
        """
        try:
            response = await self._rest_request_auth("GET", f"/v1/orders?status={status}")
            if response.get("success"):
                return response.get("data", [])
            return []
        except Exception as e:
            self.logger.warning(f"Failed to get orders: {e}")
            return []
    
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
        
        # Проверяем время до экспирации
        if market.ends_at and self.config.min_time_to_expiry_minutes > 0:
            now = datetime.now(market.ends_at.tzinfo) if market.ends_at.tzinfo else datetime.now()
            time_to_expiry = (market.ends_at - now).total_seconds() / 60
            if time_to_expiry < self.config.min_time_to_expiry_minutes:
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
            # Проверяем есть ли незахеджированная позиция (тогда нужен только hedge, не лимитки)
            state = self.markets.get(market.market_id)
            if state and state.has_unhedged_position:
                self.logger.info(f"  ⏭️  Skipping: market has unhedged position (hedge only)")
                return []
            
            # НОВОЕ: Проверяем есть ли ЛЮБАЯ позиция (включая delta-neutral)
            # Если stop_new_orders_after_fill включен - не выставляем новые лимитки
            if self.config.stop_new_orders_after_fill:
                has_any_position = await self._has_any_position(market.market_id)
                if has_any_position:
                    self.logger.info(f"  ⏭️  Skipping: already have position (stop_new_orders_after_fill=True)")
                    return []
            
            # ВАЖНО: Перед размещением ордеров проверяем существующие позиции на бирже
            # Это нужно для случаев когда рынок был удалён из tracking (ротация),
            # но позиция на бирже осталась
            existing_position = await self._check_existing_position(market)
            if existing_position:
                position_info, other_outcome = existing_position
                self.logger.info(f"  ⚠️  Found existing position: {position_info['name']} ${position_info['value_usd']:.2f}")
                self.logger.info(f"  🛡️ Placing hedge order instead of delta-neutral pair")
                
                # Размещаем только hedge ордер
                market_price = float(other_outcome.ask_price) if other_outcome.ask_price else 0.5
                size_usd = position_info['value_usd']
                size_wei = int(Decimal(str(size_usd / market_price)) * WEI_MULTIPLIER)
                
                order = await self._place_single_order(
                    market=market,
                    outcome=other_outcome,
                    side=Side.BUY,
                    price=Decimal(str(market_price)),
                    size_wei=size_wei
                )
                
                if order:
                    placed_orders.append(order)
                    self.logger.info(f"  ✅ Hedge order placed: {other_outcome.name} @ ${market_price:.4f}")
                
                # Добавляем в tracking с флагом unhedged
                if market.market_id not in self.markets:
                    self.markets[market.market_id] = MarketState(
                        market=market,
                        entered_at=datetime.now(),
                        last_rebalance=datetime.now(),
                        has_unhedged_position=True
                    )
                else:
                    self.markets[market.market_id].has_unhedged_position = True
                    self.markets[market.market_id].last_rebalance = datetime.now()
                
                return placed_orders
            
            # Проверяем лимит позиции
            current_position = state.position_usd if state else 0.0
            
            if current_position >= self.config.max_position_usd:
                self.logger.info(f"  ⚠️  Position limit reached: ${current_position:.2f} >= ${self.config.max_position_usd:.2f}")
                return []
            
            # Сколько ещё можем вложить
            remaining_budget = self.config.max_position_usd - current_position
            order_size = min(self.config.order_size_usd, remaining_budget / 2)  # /2 т.к. 2 ордера
            
            if order_size < 1.0:  # Минимум $1
                self.logger.info(f"  ⚠️  Remaining budget too small: ${remaining_budget:.2f}")
                return []
            
            # Находим outcomes по индексу (index 0 и 1)
            outcomes_by_index = {o.index: o for o in market.outcomes}
            
            outcome_0 = outcomes_by_index.get(0)
            outcome_1 = outcomes_by_index.get(1)
            
            if not outcome_0 or not outcome_1:
                if len(market.outcomes) >= 2:
                    outcome_0 = market.outcomes[0]
                    outcome_1 = market.outcomes[1]
                else:
                    self.logger.warning(f"  ⚠️  Need at least 2 outcomes, got {len(market.outcomes)}")
                    return []
            
            self.logger.info(f"  🎯 Outcomes: [{outcome_0.name}] vs [{outcome_1.name}]")
            self.logger.info(f"  💰 Position: ${current_position:.2f} / ${self.config.max_position_usd:.2f} (order: ${order_size:.2f})")
            
            # Рассчитываем mid price из chancePercentage
            mid_price = Decimal(str(market.chance_percentage / 100.0))
            
            # Пересчитываем размеры с учётом лимита
            # Выбираем spread в зависимости от режима
            if self.config.aggressive_pricing:
                spread = Decimal("0.005")  # 0.5% - очень близко к рынку для быстрых fills
                self.logger.info(f"  ⚡ AGGRESSIVE mode: 0.5% spread")
            else:
                spread = Decimal(str(self.config.target_spread))
            
            bid_price = max(Decimal("0.01"), min(Decimal("0.99"), mid_price - spread))
            ask_price = max(Decimal("0.01"), min(Decimal("0.99"), mid_price + spread))
            
            bid_size_wei = int((Decimal(str(order_size)) / bid_price) * WEI_MULTIPLIER)
            ask_size_wei = int((Decimal(str(order_size)) / ask_price) * WEI_MULTIPLIER)
            
            # Delta-neutral цена для второго исхода
            outcome_1_price = max(Decimal("0.01"), min(Decimal("0.99"), Decimal("1") - ask_price))
            
            self.logger.info(f"  📊 DELTA-NEUTRAL STRATEGY:")
            self.logger.info(f"     Mid price: {mid_price:.4f} (from probability {market.chance_percentage:.0f}%)")
            self.logger.info(f"     Spread: {float(spread):.2%}")
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
                self.markets[market.market_id] = MarketState(
                    market=market,
                    entered_at=datetime.now()  # Время входа для ротации
                )
            
            self.markets[market.market_id].our_orders = placed_orders
            self.markets[market.market_id].last_rebalance = datetime.now()
            self.markets[market.market_id].last_mid_price = mid_price  # Сохраняем цену для smart rebalance
            
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
    
    def _calculate_market_entry_loss(
        self, 
        filled_price: Decimal, 
        market_price: Decimal
    ) -> float:
        """
        Рассчитать убыток при рыночном входе для delta-neutral
        
        Если одна сторона куплена по filled_price, а вторую берём по market_price:
        Total cost = filled_price + market_price
        Guaranteed payout = 1.00
        Loss = (total_cost - 1.0) / 1.0 * 100%
        """
        total_cost = float(filled_price) + float(market_price)
        if total_cost <= 1.0:
            return 0.0  # Нет убытка, есть профит
        return (total_cost - 1.0) * 100  # Убыток в %
    
    async def _has_any_position(self, market_id: str) -> bool:
        """
        Проверить есть ли ЛЮБАЯ позиция на рынке (включая delta-neutral)
        
        Используется для stop_new_orders_after_fill - если уже есть позиция,
        не выставлять новые лимитки чтобы не наращивать позицию.
        
        Returns:
            True если есть позиция с value > $0.5
        """
        try:
            positions = await self.graphql_client.get_positions()
            if not positions:
                return False
            
            for pos in positions:
                market_info = pos.get("market", {})
                pos_market_id = str(pos.get("marketId") or market_info.get("id") or "")
                
                if pos_market_id == market_id:
                    value_raw = pos.get("valueUsd") or 0
                    try:
                        value = float(str(value_raw).replace(",", "."))
                    except:
                        value = 0
                    
                    if value > 0.5:  # Минимальный порог $0.5
                        return True
            
            return False
            
        except Exception as e:
            self.logger.debug(f"_has_any_position error: {e}")
            return False
    
    async def _check_existing_position(self, market: MarketData) -> Optional[tuple[dict, OutcomeData]]:
        """
        Проверить есть ли существующая незахеджированная позиция на рынке
        
        Возвращает:
            (position_info, opposite_outcome) если нужен hedge
            None если позиции нет или уже delta-neutral
            
        Raises:
            Exception при ошибке API (чтобы не удалять рынок при проблемах с сетью)
        """
        try:
            positions = await self.graphql_client.get_positions()
            
            # None означает ошибку API - не можем определить состояние позиции
            if positions is None:
                raise Exception("API error: cannot determine position state")
            
            if not positions:
                return None
            
            # Фильтруем позиции для этого рынка
            market_positions = []
            for pos in positions:
                market_info = pos.get("market", {})
                pos_market_id = str(pos.get("marketId") or market_info.get("id") or "")
                if pos_market_id == market.market_id:
                    market_positions.append(pos)
            
            if not market_positions:
                return None
            
            # Парсим позиции
            outcome_positions = {}
            for pos in market_positions:
                outcome_info = pos.get("outcome") or {}
                token_id = str(
                    outcome_info.get("onChainId") or
                    outcome_info.get("tokenId") or
                    pos.get("tokenId") or
                    ""
                )
                outcome_name = outcome_info.get("name") or "Unknown"
                
                value_usd_raw = pos.get("valueUsd") or 0
                try:
                    value_usd = float(str(value_usd_raw).replace(",", "."))
                except:
                    value_usd = 0
                
                quantity_raw = pos.get("amount") or pos.get("shares") or 0
                try:
                    quantity = float(str(quantity_raw).replace(",", "."))
                    if quantity > 1e15:
                        quantity = quantity / WEI_MULTIPLIER
                except:
                    quantity = 0
                
                if token_id and (quantity > 0 or value_usd > 0):
                    outcome_positions[token_id] = {
                        "name": outcome_name,
                        "value_usd": value_usd,
                        "quantity": quantity,
                        "token_id": token_id
                    }
            
            # Если 2+ сторон - уже delta-neutral, не нужен hedge
            if len(outcome_positions) >= 2:
                return None
            
            # Если 0 позиций - нет существующей позиции
            if len(outcome_positions) == 0:
                return None
            
            # Одна сторона - нужен hedge
            filled_token_id = list(outcome_positions.keys())[0]
            filled_pos = outcome_positions[filled_token_id]
            
            # Минимальный размер ордера
            MIN_ORDER_VALUE_USD = 0.9
            if filled_pos['value_usd'] < MIN_ORDER_VALUE_USD:
                return None  # Слишком маленькая позиция
            
            # Находим противоположный outcome
            other_outcome = None
            for outcome in market.outcomes:
                if outcome.on_chain_id != filled_token_id:
                    other_outcome = outcome
                    break
            
            if not other_outcome or not other_outcome.on_chain_id:
                return None
            
            return (filled_pos, other_outcome)
            
        except Exception as e:
            self.logger.debug(f"  Position check error: {e}")
            return None
    
    async def _ensure_position_hedged(self, market_id: str, market: MarketData) -> bool:
        """
        Проверить есть ли незахеджированная позиция и разместить hedge ордер
        
        Вызывается при ребалансировке ПЕРЕД размещением новых ордеров,
        чтобы существующая позиция не осталась без защиты.
        
        Returns:
            True если был размещён hedge ордер (не нужны дополнительные лимитки)
            False если позиция уже delta-neutral или нет позиции
        """
        try:
            # Получаем текущие позиции
            positions = await self.graphql_client.get_positions()
            if not positions:
                return False
            
            # Фильтруем позиции для этого рынка
            market_positions = []
            for pos in positions:
                market_info = pos.get("market", {})
                pos_market_id = str(pos.get("marketId") or market_info.get("id") or "")
                if pos_market_id == market_id:
                    market_positions.append(pos)
            
            if not market_positions:
                return False
            
            # Парсим позиции
            outcome_positions = {}
            for pos in market_positions:
                outcome_info = pos.get("outcome") or {}
                token_id = str(outcome_info.get("onChainId") or pos.get("tokenId") or "")
                outcome_name = outcome_info.get("name") or "Unknown"
                
                value_usd_raw = pos.get("valueUsd") or 0
                try:
                    value_usd = float(str(value_usd_raw).replace(",", "."))
                except:
                    value_usd = 0
                
                quantity_raw = pos.get("amount") or pos.get("shares") or 0
                try:
                    quantity = float(str(quantity_raw).replace(",", "."))
                    if quantity > 1e15:
                        quantity = quantity / WEI_MULTIPLIER
                except:
                    quantity = 0
                
                if token_id and (quantity > 0 or value_usd > 0):
                    outcome_positions[token_id] = {
                        "name": outcome_name,
                        "value_usd": value_usd,
                        "quantity": quantity,
                        "token_id": token_id
                    }
            
            # Если 2+ сторон - уже delta-neutral, можно размещать обычные лимитки
            if len(outcome_positions) >= 2:
                # Сбрасываем флаг unhedged если был установлен
                if market_id in self.markets and self.markets[market_id].has_unhedged_position:
                    self.markets[market_id].has_unhedged_position = False
                return False
            
            # Если 1 сторона - нужен hedge
            if len(outcome_positions) == 1:
                filled_token_id = list(outcome_positions.keys())[0]
                filled_pos = outcome_positions[filled_token_id]
                
                # Минимальный размер ордера
                MIN_ORDER_VALUE_USD = 0.9
                if filled_pos['value_usd'] < MIN_ORDER_VALUE_USD:
                    return False  # Слишком маленькая позиция, пусть размещает обычные лимитки
                
                # Находим противоположный outcome
                other_outcome = None
                for outcome in market.outcomes:
                    if outcome.on_chain_id != filled_token_id:
                        other_outcome = outcome
                        break
                
                if not other_outcome or not other_outcome.on_chain_id:
                    return False
                
                # Размещаем hedge ордер
                market_price = float(other_outcome.ask_price) if other_outcome.ask_price else 0.5
                size_usd = filled_pos['value_usd']
                size_wei = int(Decimal(str(size_usd / market_price)) * WEI_MULTIPLIER)
                
                self.logger.info(f"  🛡️ Position needs hedge: {filled_pos['name']} ${size_usd:.2f} → placing {other_outcome.name} order")
                
                order = await self._place_single_order(
                    market=market,
                    outcome=other_outcome,
                    side=Side.BUY,
                    price=Decimal(str(market_price)),
                    size_wei=size_wei
                )
                if order:
                    self.logger.info(f"  ✅ Hedge order placed: {other_outcome.name} @ ${market_price:.4f}")
                    return True  # Hedge размещён, не нужны доп. лимитки
                
                return False  # Не удалось разместить hedge
            
            # len(outcome_positions) == 0 - нет валидных позиций
            return False
                    
        except Exception as e:
            self.logger.debug(f"  Position hedge check error: {e}")
            return False
    
    async def _check_and_handle_partial_fills(self, market_id: str, state: MarketState) -> None:
        """
        Проверить частичное исполнение и при необходимости войти по рынку
        
        Если включен market_fallback и одна сторона исполнилась:
        1. Получить текущую рыночную цену другой стороны
        2. Рассчитать убыток при рыночном входе
        3. Если убыток <= max_market_loss_percent, войти по рынку
        4. Иначе оставить лимитку
        """
        if not self.config.enable_market_fallback:
            return
        
        # TODO: Для полной реализации нужно:
        # 1. Запросить статус ордеров через REST API GET /v1/orders
        # 2. Определить какая сторона исполнилась
        # 3. Получить текущий orderbook для рыночной цены
        # 4. Выполнить market order если выгодно
        #
        # Пока это placeholder для будущей реализации
        pass
    
    async def _rebalance_existing_markets(self) -> None:
        """Ребалансировка существующих рынков"""
        for market_id, state in list(self.markets.items()):
            try:
                # Проверяем ротацию (слишком долго на рынке)
                if self.config.max_market_hold_minutes > 0 and state.entered_at:
                    hold_minutes = (datetime.now() - state.entered_at).total_seconds() / 60
                    if hold_minutes >= self.config.max_market_hold_minutes:
                        self.logger.info(f"🔄 ROTATION: {state.market.title[:40]}...")
                        self.logger.info(f"  ⏰ Held for {hold_minutes:.0f} min >= {self.config.max_market_hold_minutes} min limit")
                        
                        # ВАЖНО: Перед удалением проверяем есть ли незахеджированная позиция
                        # Если есть - оставляем в tracking и размещаем/обновляем hedge
                        try:
                            existing_position = await self._check_existing_position(state.market)
                            if existing_position:
                                position_info, other_outcome = existing_position
                                self.logger.info(f"  ⚠️  Has unhedged position: {position_info['name']} ${position_info['value_usd']:.2f}")
                                self.logger.info(f"  🛡️ Keeping market for hedge management (not rotating)")
                                state.has_unhedged_position = True
                                # Не удаляем рынок - нужно поддерживать hedge
                                continue
                            
                            # Если рынок уже помечен как hedge-only, не удаляем его
                            if state.has_unhedged_position:
                                self.logger.info(f"  🛡️ Keeping hedge-only market (not rotating)")
                                continue
                            
                            await self.cancel_old_orders(market_id)
                            del self.markets[market_id]
                            continue
                        except Exception as e:
                            # При ошибке API не удаляем рынок
                            self.logger.warning(f"  ⚠️ Cannot verify position (API error), keeping market: {e}")
                            continue
                
                # Проверяем время до экспирации
                if state.market.ends_at and self.config.min_time_to_expiry_minutes > 0:
                    now = datetime.now(state.market.ends_at.tzinfo) if state.market.ends_at.tzinfo else datetime.now()
                    time_to_expiry = (state.market.ends_at - now).total_seconds() / 60
                    if time_to_expiry < self.config.min_time_to_expiry_minutes:
                        self.logger.info(f"⏰ EXPIRY CLOSE: {state.market.title[:40]}...")
                        self.logger.info(f"  ⚠️  Only {time_to_expiry:.0f} min left < {self.config.min_time_to_expiry_minutes} min threshold")
                        await self.cancel_old_orders(market_id)
                        del self.markets[market_id]
                        continue
                
                # Проверяем нужна ли ребалансировка
                if state.last_rebalance:
                    elapsed = (datetime.now() - state.last_rebalance).total_seconds()
                    if elapsed < self.config.rebalance_interval_sec:
                        continue
                
                # Получаем актуальные данные рынка для проверки цены
                updated = await self.graphql_client.get_market(market_id)
                if not updated:
                    self.logger.warning(f"  ⚠️  Could not fetch market {market_id} - skipping")
                    continue
                
                # Smart rebalance: пропускаем если цена не изменилась значительно
                if self.config.skip_rebalance_if_price_stable and state.last_mid_price is not None:
                    current_mid = Decimal(str(updated.chance_percentage / 100.0))
                    price_change = abs(float(current_mid - state.last_mid_price))
                    
                    if price_change < self.config.price_change_threshold:
                        self.logger.info(f"⏭️  Skip rebalance: {state.market.title[:30]}... (price Δ {price_change:.2%} < {self.config.price_change_threshold:.2%})")
                        # Обновляем время и данные рынка
                        state.last_rebalance = datetime.now()
                        state.market = updated
                        continue
                    else:
                        self.logger.info(f"🔄 Rebalancing: {state.market.title[:40]}... (price moved {price_change:.2%})")
                else:
                    self.logger.info(f"🔄 Rebalancing: {state.market.title[:40]}...")
                
                # Проверяем частичное исполнение (market fallback)
                await self._check_and_handle_partial_fills(market_id, state)
                
                # Отменяем старые
                await self.cancel_old_orders(market_id)
                
                # Проверяем что рынок ещё подходит (updated уже получен выше)
                if updated and self._is_market_suitable(updated):
                    state.market = updated
                    
                    # ВАЖНО: Проверяем позиции и размещаем hedge если нужно
                    # Если hedge был размещён - не размещаем дополнительные лимитки
                    hedge_placed = await self._ensure_position_hedged(market_id, updated)
                    
                    if hedge_placed:
                        self.logger.info(f"  ⏭️ Skipping limit orders (hedge already placed)")
                        state.last_rebalance = datetime.now()
                    else:
                        await self.place_limit_orders(updated)
                else:
                    # Рынок больше не подходит, НО сначала проверяем позицию!
                    existing_position = await self._check_existing_position(updated or state.market)
                    
                    if existing_position:
                        position_info, other_outcome = existing_position
                        self.logger.info(f"  ⚠️  Market unsuitable but has position: {position_info['name']} ${position_info['value_usd']:.2f}")
                        self.logger.info(f"  🛡️ Keeping market for hedge management only")
                        
                        # Оставляем рынок в tracking только для hedge
                        state.has_unhedged_position = True
                        state.market = updated or state.market
                        
                        # Размещаем hedge ордер
                        market_price = float(other_outcome.ask_price) if other_outcome.ask_price else 0.5
                        size_usd = position_info['value_usd']
                        size_wei = int(Decimal(str(size_usd / market_price)) * WEI_MULTIPLIER)
                        
                        order = await self._place_single_order(
                            market=state.market,
                            outcome=other_outcome,
                            side=Side.BUY,
                            price=Decimal(str(market_price)),
                            size_wei=size_wei
                        )
                        if order:
                            self.logger.info(f"  ✅ Hedge order placed: {other_outcome.name} @ ${market_price:.4f}")
                        
                        state.last_rebalance = datetime.now()
                    else:
                        # Нет позиции - можно безопасно удалить
                        # НО: не удаляем если рынок помечен как hedge-only (has_unhedged_position)
                        # Это защита от удаления при временных ошибках API
                        if state.has_unhedged_position:
                            self.logger.info(f"  ⚠️ Keeping hedge-only market (API may have returned stale data)")
                            state.last_rebalance = datetime.now()
                        else:
                            self.logger.info(f"  📤 Market no longer suitable (no position), removing...")
                            del self.markets[market_id]
                
                await asyncio.sleep(self.config.api_delay_sec)
                
            except Exception as e:
                # При ошибках API НЕ удаляем рынок - лучше оставить в tracking
                self.logger.error(f"❌ Rebalance error for {market_id}: {e}")
                # Обновляем last_rebalance чтобы не спамить запросами
                if market_id in self.markets:
                    self.markets[market_id].last_rebalance = datetime.now()
    
    async def _recover_open_orders(self) -> set[str]:
        """
        Загрузить существующие открытые ордера при перезапуске
        
        Returns:
            Set of market_ids that already have open orders
        """
        self.logger.info("🔍 Loading existing open orders...")
        markets_with_orders: set[str] = set()
        
        try:
            orders = await self.graphql_client.get_open_orders("OPEN")
            
            if not orders:
                self.logger.info("  ✅ No existing open orders")
                return markets_with_orders
            
            self.logger.info(f"  📋 Found {len(orders)} open order(s)")
            
            for order_wrapper in orders:
                # API может вернуть {order: {...}, ...} или напрямую {...}
                order = order_wrapper.get("order", order_wrapper)
                
                order_id = str(order_wrapper.get("id", order.get("id", order.get("orderId", ""))))
                
                # Market ID может быть в разных местах
                market_id = str(
                    order_wrapper.get("marketId") or
                    order_wrapper.get("market", {}).get("id") or
                    order.get("marketId") or
                    ""
                )
                
                token_id = str(order.get("tokenId", ""))
                
                # Price может быть в разных форматах
                price_raw = order_wrapper.get("pricePerShare") or order.get("pricePerShare") or 0
                try:
                    # pricePerShare обычно в wei (10^18), конвертируем в десятичную
                    price_val = int(price_raw) / WEI_MULTIPLIER if price_raw else 0
                except (ValueError, TypeError):
                    price_val = float(price_raw) if price_raw else 0
                
                if market_id:
                    markets_with_orders.add(market_id)
                
                # Добавляем в active_orders для отслеживания
                if order_id:
                    self.active_orders[order_id] = OrderInfo(
                        order_id=order_id,
                        order_hash=order.get("hash", order_wrapper.get("orderHash", "")),
                        market_id=market_id,
                        token_id=token_id,
                        side=Side.BUY,  # Предполагаем BUY для delta-neutral
                        price=Decimal(str(price_val)) if price_val else Decimal("0"),
                        size_wei=0,
                        status=OrderStatus.OPEN,
                        created_at=datetime.now(),
                        expires_at=datetime.now() + timedelta(minutes=self.config.order_expiry_minutes)
                    )
                    self.logger.info(f"    📝 Order {order_id}: market={market_id[:10] if market_id else 'N/A'}... token={token_id[:10] if token_id else 'N/A'}...")
            
            # Добавляем рынки с ордерами в self.markets для отслеживания
            # ВАЖНО: устанавливаем last_rebalance чтобы НЕ делать немедленную ребалансировку!
            for market_id in markets_with_orders:
                if market_id not in self.markets:
                    # Получаем данные рынка
                    try:
                        market = await self.graphql_client.get_market(market_id)
                        if market:
                            self.markets[market_id] = MarketState(
                                market=market,
                                entered_at=datetime.now(),
                                last_rebalance=datetime.now()  # ВАЖНО: не ребалансировать сразу!
                            )
                    except Exception as e:
                        self.logger.debug(f"    Could not load market {market_id}: {e}")
            
            self.logger.info(f"  ✅ Loaded {len(markets_with_orders)} market(s) with existing orders (will NOT rebalance immediately)")
            return markets_with_orders
            
        except Exception as e:
            self.logger.warning(f"⚠️  Failed to load open orders: {e}")
            import traceback
            self.logger.debug(traceback.format_exc())
            return markets_with_orders
    
    async def _recover_positions(self) -> None:
        """
        Восстановить позиции при перезапуске бота
        
        1. Получить открытые позиции
        2. Найти незахеджированные (только одна сторона)
        3. Проверить нет ли уже открытого hedge ордера
        4. Попробовать закрыть delta-neutral:
           - По рынку если убыток < порога
           - Лимиткой если убыток > порога
        """
        self.logger.info("🔍 Recovering existing positions...")
        
        # Собираем token_ids из существующих открытых ордеров
        # чтобы не дублировать hedge ордера
        self._existing_order_tokens: set[str] = set()
        for order_id, order_info in self.active_orders.items():
            if order_info.token_id:
                self._existing_order_tokens.add(order_info.token_id)
        
        try:
            positions = await self.graphql_client.get_positions()
            
            if not positions:
                self.logger.info("  ✅ No existing positions found")
                return
            
            self.logger.info(f"  📊 Found {len(positions)} position(s)")
            
            # Логируем первую позицию для отладки структуры данных
            if positions:
                self.logger.info(f"  🔍 Sample position keys: {list(positions[0].keys())}")
                # Логируем полную структуру первой позиции (для отладки)
                pos_sample = positions[0]
                self.logger.info(f"  🔍 Position sample: valueUsd={pos_sample.get('valueUsd')}, market={pos_sample.get('market', {}).get('title', 'N/A')[:30]}")
                if pos_sample.get("outcome"):
                    self.logger.info(f"  🔍 Outcome keys: {list(pos_sample.get('outcome', {}).keys())}")
                else:
                    self.logger.info(f"  🔍 No 'outcome' key found. Available keys: {list(pos_sample.keys())}")
            
            # Группируем позиции по market_id
            # API возвращает позиции в формате:
            # {"id": "base64...", "valueUsd": "18.76", "market": {"id": "6126", "title": "..."}, 
            #  "outcome": {"id": "...", "name": "YES", "onChainId": "..."}, "shares": "50.7", ...}
            positions_by_market: dict[str, list[dict]] = {}
            for pos in positions:
                # Market ID может быть в разных местах
                market_info = pos.get("market", {})
                market_id = str(
                    pos.get("marketId") or 
                    market_info.get("id") or 
                    ""
                )
                
                if market_id:
                    if market_id not in positions_by_market:
                        positions_by_market[market_id] = []
                    positions_by_market[market_id].append(pos)
            
            self.logger.info(f"  📈 Positions across {len(positions_by_market)} market(s)")
            
            for market_id, market_positions in positions_by_market.items():
                await self._analyze_and_hedge_position(market_id, market_positions)
                await asyncio.sleep(self.config.api_delay_sec)
            
            self.logger.info("  ✅ Position recovery complete")
            
        except Exception as e:
            self.logger.error(f"❌ Position recovery error: {e}")
            import traceback
            self.logger.error(traceback.format_exc())
    
    async def _analyze_and_hedge_position(self, market_id: str, positions: list[dict]) -> None:
        """
        Анализировать позицию и захеджировать если нужно
        
        API формат позиции:
        {
            "id": "base64...",
            "valueUsd": "18.76",
            "market": {"id": "6126", "title": "..."},
            "outcome": {"id": "...", "name": "YES", "onChainId": "12345..."},
            "shares": "50.7" или "quantity": 50.7,
            "avgPrice": "0.394" или в центах "39.4"
        }
        """
        try:
            # Получаем данные рынка
            market = await self.graphql_client.get_market(market_id)
            if not market:
                self.logger.warning(f"  ⚠️  Market {market_id} not found")
                return
            
            market_title = market.title[:40] if market.title else market_id[:20]
            self.logger.info(f"  📈 Analyzing: {market_title}...")
            
            # Парсим позиции - определяем какие outcomes у нас есть
            # API может возвращать данные в разных форматах
            outcome_positions = {}
            
            for pos in positions:
                # Логируем структуру для отладки (только первую позицию в маркете)
                if not outcome_positions:
                    self.logger.debug(f"    Raw position data: {json.dumps(pos, default=str)[:300]}")
                
                # Получаем outcome info - может быть в разных местах
                outcome_info = pos.get("outcome") or pos.get("outcomeData") or {}
                
                # Token ID / onChainId может быть в разных местах
                token_id = str(
                    outcome_info.get("onChainId") or
                    outcome_info.get("tokenId") or
                    outcome_info.get("id") or
                    pos.get("tokenId") or
                    pos.get("onChainId") or
                    pos.get("outcomeId") or
                    ""
                )
                
                # Outcome name
                outcome_name = (
                    outcome_info.get("name") or
                    outcome_info.get("title") or
                    pos.get("outcomeName") or
                    "Unknown"
                )
                
                # Получаем количество (shares) - много возможных названий
                # ВАЖНО: amount обычно в wei (10^18), нужно конвертировать
                quantity_raw = (
                    pos.get("shares") or 
                    pos.get("quantity") or 
                    pos.get("size") or
                    pos.get("amount") or
                    pos.get("balance") or
                    0
                )
                try:
                    quantity = float(str(quantity_raw).replace(",", "."))
                    # Если количество очень большое (> 10^15), скорее всего это wei
                    if quantity > 1e15:
                        quantity = quantity / WEI_MULTIPLIER
                except (ValueError, TypeError):
                    quantity = 0
                
                # Получаем USD value (нужно до avgPrice для вычисления)
                value_usd_raw = pos.get("valueUsd") or pos.get("value") or pos.get("totalValue") or 0
                try:
                    value_usd = float(str(value_usd_raw).replace(",", "."))
                except (ValueError, TypeError):
                    value_usd = 0
                
                # Получаем цену входа - много возможных названий
                avg_price_raw = (
                    pos.get("avgPrice") or 
                    pos.get("averagePrice") or
                    pos.get("entryPrice") or 
                    pos.get("price") or
                    pos.get("costBasis") or
                    0
                )
                try:
                    avg_price = float(str(avg_price_raw).replace(",", "."))
                    # Если цена > 1, вероятно это в центах (39.4 = $0.394)
                    if avg_price > 1:
                        avg_price = avg_price / 100.0
                except (ValueError, TypeError):
                    avg_price = 0
                
                # Если avgPrice не найден, но есть valueUsd и quantity - вычисляем
                if avg_price == 0 and quantity > 0 and value_usd > 0:
                    avg_price = value_usd / quantity
                
                # Если нет token_id, попробуем использовать outcome name как идентификатор
                position_key = token_id or outcome_name
                
                # Если есть хоть какие-то данные - добавляем
                if position_key and position_key != "Unknown" and (quantity > 0 or value_usd > 0):
                    outcome_positions[position_key] = {
                        "quantity": quantity,
                        "entry_price": avg_price,
                        "value_usd": value_usd,
                        "token_id": token_id,
                        "name": outcome_name
                    }
                    self.logger.info(f"    Position: {outcome_name} | Qty: {quantity:.2f} | Avg: ${avg_price:.4f} | Value: ${value_usd:.2f}")
                else:
                    # Логируем что не удалось распарсить
                    self.logger.debug(f"    Could not parse position: key={position_key}, qty={quantity}, value={value_usd}")
            
            if len(outcome_positions) == 0:
                self.logger.info(f"    No active positions (could not parse data)")
                # Логируем доступные поля для отладки
                if positions:
                    self.logger.info(f"    Debug - first position keys: {list(positions[0].keys())}")
                return
            
            if len(outcome_positions) >= 2:
                self.logger.info(f"    ✅ Already delta-neutral (has {len(outcome_positions)} sides)")
                # Обновляем state если рынок есть
                if market_id in self.markets:
                    self.markets[market_id].filled_outcome_0 = True
                    self.markets[market_id].filled_outcome_1 = True
                    self.markets[market_id].has_unhedged_position = False  # Сбрасываем флаг
                else:
                    # Добавляем в tracking как delta-neutral
                    self.markets[market_id] = MarketState(
                        market=market,
                        entered_at=datetime.now(),
                        last_rebalance=datetime.now(),
                        filled_outcome_0=True,
                        filled_outcome_1=True,
                        has_unhedged_position=False
                    )
                return
            
            # Только одна сторона - нужно захеджировать
            filled_key = list(outcome_positions.keys())[0]
            filled_pos = outcome_positions[filled_key]
            filled_token_id = filled_pos.get("token_id", "")
            filled_name = filled_pos.get("name", "Unknown")
            
            self.logger.info(f"    ⚠️  UNHEDGED: {filled_name} | Entry: ${filled_pos['entry_price']:.4f} | Qty: {filled_pos['quantity']:.2f}")
            
            # Находим противоположный outcome
            # Сначала пробуем по token_id, потом по имени
            other_outcome = None
            for outcome in market.outcomes:
                # По token_id
                if filled_token_id and outcome.on_chain_id != filled_token_id:
                    other_outcome = outcome
                    break
                # По имени (если нет token_id)
                if not filled_token_id and outcome.name.lower() != filled_name.lower():
                    other_outcome = outcome
                    break
            
            if not other_outcome:
                # Если рынок бинарный, просто берём другой outcome
                if len(market.outcomes) == 2:
                    for outcome in market.outcomes:
                        if outcome.name.lower() != filled_name.lower():
                            other_outcome = outcome
                            break
            
            if not other_outcome:
                self.logger.warning(f"    ❌ Could not find opposite outcome for {filled_name}")
                return
            
            # Проверяем что у opposite outcome есть on_chain_id для размещения ордера
            if not other_outcome.on_chain_id:
                self.logger.warning(f"    ❌ Opposite outcome {other_outcome.name} has no on_chain_id")
                return
            
            # Проверяем нет ли уже открытого ордера на этот token (hedge уже размещён)
            existing_tokens = getattr(self, '_existing_order_tokens', set())
            if other_outcome.on_chain_id in existing_tokens:
                self.logger.info(f"    ✅ Hedge order already exists for {other_outcome.name} - skipping")
                return
            
            # Текущая рыночная цена другой стороны (ask price)
            market_price = float(other_outcome.ask_price) if other_outcome.ask_price else 0.5
            
            # Рассчитываем убыток при рыночном входе
            filled_price = Decimal(str(filled_pos['entry_price']))
            loss_percent = self._calculate_market_entry_loss(filled_price, Decimal(str(market_price)))
            
            self.logger.info(f"    Hedge target: {other_outcome.name} @ ${market_price:.4f} (ask)")
            self.logger.info(f"    Entry + Market = ${float(filled_price) + market_price:.4f} | Loss: {loss_percent:.2f}%")
            
            # Минимальный размер ордера на Predict.fun = $0.9
            MIN_ORDER_VALUE_USD = 0.9
            
            if loss_percent <= self.config.max_market_loss_percent:
                # Входим по рынку - убыток приемлемый
                self.logger.info(f"    ✅ Loss {loss_percent:.2f}% <= {self.config.max_market_loss_percent}%, entering at MARKET")
                
                # Размер = стоимость первой позиции, чтобы суммы были равны
                size_usd = filled_pos['value_usd'] if filled_pos['value_usd'] > 0 else (filled_pos['quantity'] * filled_pos['entry_price'])
                
                # Проверяем минимальный размер ордера
                if size_usd < MIN_ORDER_VALUE_USD:
                    self.logger.warning(f"    ⚠️  Position value ${size_usd:.2f} < min ${MIN_ORDER_VALUE_USD} - skipping hedge")
                    return
                
                size_wei = int(Decimal(str(size_usd / market_price)) * WEI_MULTIPLIER)
                
                order = await self._place_single_order(
                    market=market,
                    outcome=other_outcome,
                    side=Side.BUY,
                    price=Decimal(str(market_price)),
                    size_wei=size_wei
                )
                if order:
                    self.logger.info(f"    ✅ Hedge MARKET order placed for ${size_usd:.2f}!")
                    # ВАЖНО: добавляем рынок в tracking чтобы не размещать дубли
                    if market_id not in self.markets:
                        self.markets[market_id] = MarketState(
                            market=market,
                            entered_at=datetime.now(),
                            last_rebalance=datetime.now(),
                            has_unhedged_position=True  # Флаг что есть незахеджированная позиция
                        )
            else:
                # Ставим лимитку - убыток слишком большой
                # Рассчитываем цену лимитки: filled_price + limit_price <= 1 + max_loss%
                max_limit_price = 1.0 + (self.config.max_market_loss_percent / 100) - float(filled_price)
                limit_price = Decimal(str(max(0.01, min(0.99, max_limit_price))))
                
                self.logger.info(f"    📝 Loss {loss_percent:.2f}% > {self.config.max_market_loss_percent}%, placing LIMIT @ ${float(limit_price):.4f}")
                
                size_usd = filled_pos['value_usd'] if filled_pos['value_usd'] > 0 else (filled_pos['quantity'] * filled_pos['entry_price'])
                
                # Проверяем минимальный размер ордера
                if size_usd < MIN_ORDER_VALUE_USD:
                    self.logger.warning(f"    ⚠️  Position value ${size_usd:.2f} < min ${MIN_ORDER_VALUE_USD} - skipping hedge")
                    return
                
                size_wei = int(Decimal(str(size_usd / float(limit_price))) * WEI_MULTIPLIER)
                
                order = await self._place_single_order(
                    market=market,
                    outcome=other_outcome,
                    side=Side.BUY,
                    price=limit_price,
                    size_wei=size_wei
                )
                if order:
                    self.logger.info(f"    ✅ Hedge LIMIT order placed for ${size_usd:.2f}!")
                    # ВАЖНО: добавляем рынок в tracking чтобы не размещать дубли
                    if market_id not in self.markets:
                        self.markets[market_id] = MarketState(
                            market=market,
                            entered_at=datetime.now(),
                            last_rebalance=datetime.now(),
                            has_unhedged_position=True
                        )
            
        except Exception as e:
            self.logger.error(f"    ❌ Hedge error: {e}")
            import traceback
            self.logger.debug(traceback.format_exc())
    
    async def _check_orphaned_positions(self) -> None:
        """
        Периодическая проверка "осиротевших" позиций
        
        Позиция считается осиротевшей если:
        1. Она существует на бирже
        2. Её рынок НЕ отслеживается в self.markets
        3. Она не delta-neutral (только одна сторона)
        
        Вызывается каждый цикл главного loop.
        """
        try:
            positions = await self.graphql_client.get_positions()
            if not positions:
                return
            
            # Группируем позиции по market_id
            positions_by_market: dict[str, list[dict]] = {}
            for pos in positions:
                market_info = pos.get("market", {})
                market_id = str(pos.get("marketId") or market_info.get("id") or "")
                if market_id:
                    if market_id not in positions_by_market:
                        positions_by_market[market_id] = []
                    positions_by_market[market_id].append(pos)
            
            # Проверяем каждый рынок с позицией
            orphaned_count = 0
            for market_id, market_positions in positions_by_market.items():
                # Пропускаем если рынок уже отслеживается
                if market_id in self.markets:
                    continue
                
                # Проверяем количество сторон позиции
                outcome_sides = set()
                total_value = 0.0
                
                for pos in market_positions:
                    outcome_info = pos.get("outcome") or {}
                    token_id = str(outcome_info.get("onChainId") or pos.get("tokenId") or "")
                    value_raw = pos.get("valueUsd") or 0
                    try:
                        value = float(str(value_raw).replace(",", "."))
                    except:
                        value = 0
                    
                    if token_id and value > 0.5:  # Минимум $0.5
                        outcome_sides.add(token_id)
                        total_value += value
                
                # Если только одна сторона и стоимость > $0.9 - нужен hedge
                if len(outcome_sides) == 1 and total_value >= 0.9:
                    orphaned_count += 1
                    market_title = market_positions[0].get("market", {}).get("title", market_id)[:30]
                    self.logger.warning(f"🚨 ORPHANED POSITION: {market_title}... | Value: ${total_value:.2f}")
                    
                    # Пытаемся захеджировать
                    await self._hedge_orphaned_position(market_id, market_positions)
            
            if orphaned_count > 0:
                self.logger.info(f"📋 Found {orphaned_count} orphaned position(s) requiring hedge")
                
        except Exception as e:
            self.logger.debug(f"Orphaned position check error: {e}")
    
    async def _hedge_orphaned_position(self, market_id: str, positions: list[dict]) -> None:
        """Захеджировать осиротевшую позицию"""
        try:
            # Получаем данные рынка
            market = await self.graphql_client.get_market(market_id)
            if not market:
                self.logger.warning(f"  ⚠️  Could not fetch market {market_id}")
                return
            
            # Парсим позицию
            filled_token_id = None
            filled_value = 0.0
            filled_name = "Unknown"
            
            for pos in positions:
                outcome_info = pos.get("outcome") or {}
                token_id = str(outcome_info.get("onChainId") or pos.get("tokenId") or "")
                name = outcome_info.get("name") or "Unknown"
                value_raw = pos.get("valueUsd") or 0
                try:
                    value = float(str(value_raw).replace(",", "."))
                except:
                    value = 0
                
                if token_id and value > filled_value:
                    filled_token_id = token_id
                    filled_value = value
                    filled_name = name
            
            if not filled_token_id or filled_value < 0.9:
                return
            
            # Находим противоположный outcome
            other_outcome = None
            for outcome in market.outcomes:
                if outcome.on_chain_id != filled_token_id:
                    other_outcome = outcome
                    break
            
            if not other_outcome or not other_outcome.on_chain_id:
                self.logger.warning(f"  ⚠️  Could not find opposite outcome for {filled_name}")
                return
            
            # Проверяем нет ли уже ордера на этот token
            for order_info in self.active_orders.values():
                if order_info.token_id == other_outcome.on_chain_id and order_info.status == OrderStatus.OPEN:
                    self.logger.info(f"  ✅ Hedge order already exists for {other_outcome.name}")
                    return
            
            # Размещаем hedge
            market_price = float(other_outcome.ask_price) if other_outcome.ask_price else 0.5
            size_wei = int(Decimal(str(filled_value / market_price)) * WEI_MULTIPLIER)
            
            self.logger.info(f"  🛡️ Hedging orphaned {filled_name} ${filled_value:.2f} → {other_outcome.name} @ ${market_price:.4f}")
            
            order = await self._place_single_order(
                market=market,
                outcome=other_outcome,
                side=Side.BUY,
                price=Decimal(str(market_price)),
                size_wei=size_wei
            )
            
            if order:
                self.logger.info(f"  ✅ Orphaned position hedged!")
                # Добавляем рынок в tracking
                self.markets[market_id] = MarketState(
                    market=market,
                    entered_at=datetime.now(),
                    last_rebalance=datetime.now(),
                    has_unhedged_position=True
                )
                
        except Exception as e:
            self.logger.error(f"  ❌ Hedge orphaned error: {e}")
    
    def log_statistics(self) -> None:
        """Логирование статистики"""
        self.logger.info("=" * 60)
        self.logger.info("📊 STATISTICS")
        
        # Считаем типы рынков
        normal_markets = sum(1 for s in self.markets.values() if not s.has_unhedged_position)
        hedge_only_markets = sum(1 for s in self.markets.values() if s.has_unhedged_position)
        
        self.logger.info(f"  Markets: {len(self.markets)} (normal: {normal_markets}, hedge-only: {hedge_only_markets})")
        self.logger.info(f"  Orders placed: {self.orders_placed} | Filled: {self.orders_filled} | Cancelled: {self.orders_cancelled}")
        
        active = sum(1 for o in self.active_orders.values() if o.status == OrderStatus.OPEN)
        self.logger.info(f"  Active orders: {active}")
        
        # Показываем JWT статус
        jwt_created = self.graphql_client._jwt_created_at
        if jwt_created:
            age_min = (datetime.now() - jwt_created).total_seconds() / 60
            self.logger.info(f"  🔐 JWT age: {age_min:.0f} min (refresh at {self.graphql_client.JWT_REFRESH_INTERVAL_SEC/60:.0f} min)")
        else:
            self.logger.warning(f"  ⚠️ JWT not set!")
        
        # Показываем hedge-only рынки если есть
        if hedge_only_markets > 0:
            hedge_markets = [s.market.title[:25] for s in self.markets.values() if s.has_unhedged_position]
            self.logger.info(f"  🛡️ Hedge-only: {', '.join(hedge_markets)}")
        
        self.logger.info("=" * 60)
    
    def _log_config(self) -> None:
        """Логирование конфигурации при старте"""
        self.logger.info("📋 CONFIGURATION:")
        self.logger.info(f"  Strategy: DELTA-NEUTRAL (BUY both sides)")
        self.logger.info(f"  Order size: ${self.config.order_size_usd:.2f}")
        self.logger.info(f"  Target spread: {self.config.target_spread:.1%}")
        self.logger.info(f"  Aggressive pricing: {'ON (0.5% spread)' if self.config.aggressive_pricing else 'OFF'}")
        self.logger.info(f"  Stop orders after fill: {'ON (hedge only)' if self.config.stop_new_orders_after_fill else 'OFF (allow growth)'}")
        self.logger.info(f"  Max position: ${self.config.max_position_usd:.2f}")
        self.logger.info(f"  Probability filter: {self.config.min_probability:.0%} - {self.config.max_probability:.0%}")
        self.logger.info(f"  Liquidity filter: ${self.config.min_liquidity_usd:.0f} - ${self.config.max_liquidity_usd:.0f}")
        self.logger.info(f"  Rebalance interval: {self.config.rebalance_interval_sec}s ({self.config.rebalance_interval_sec//60} min)")
        self.logger.info(f"  Smart rebalance: {'ON' if self.config.skip_rebalance_if_price_stable else 'OFF'} (threshold: {self.config.price_change_threshold:.1%})")
        self.logger.info(f"  Market rotation: {self.config.max_market_hold_minutes} min (0=off)")
        self.logger.info(f"  Min time to expiry: {self.config.min_time_to_expiry_minutes} min")
        self.logger.info(f"  Market fallback: {'ON' if self.config.enable_market_fallback else 'OFF'}")
        if self.config.enable_market_fallback:
            self.logger.info(f"  Max market loss: {self.config.max_market_loss_percent:.1f}%")
        self.logger.info(f"  Position recovery: {'ON' if self.config.recover_positions_on_start else 'OFF'}")
        self.logger.info("=" * 60)
    
    async def run(self) -> None:
        """Запустить бота"""
        self.logger.info("=" * 60)
        self.logger.info("🚀 PREDICT.FUN MARKET MAKER BOT")
        self.logger.info("=" * 60)
        self._log_config()
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
                
                # Восстанавливаем состояние после перезапуска
                markets_with_orders: set[str] = set()
                if self.config.recover_positions_on_start:
                    # Сначала загружаем существующие ордера
                    markets_with_orders = await self._recover_open_orders()
                    # Затем проверяем незахеджированные позиции
                    await self._recover_positions()
                else:
                    self.logger.info("⏭️  Position recovery disabled")
                
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
                            # Пропускаем если уже есть ордера (из recovery или текущей сессии)
                            if market.market_id in self.markets:
                                continue
                            if market.market_id in markets_with_orders:
                                self.logger.debug(f"  ⏭️  Skipping {market.title[:30]}... (has existing orders)")
                                continue
                            
                            await self.place_limit_orders(market)
                            await asyncio.sleep(self.config.api_delay_sec)
                        
                        # Ребалансировка существующих позиций
                        await self._rebalance_existing_markets()
                        
                        # ВАЖНО: Проверяем осиротевшие позиции (не отслеживаемые в self.markets)
                        await self._check_orphaned_positions()
                        
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
    if os.getenv("MAX_POSITION_USD"):
        config.max_position_usd = float(os.getenv("MAX_POSITION_USD"))
    if os.getenv("ENABLE_MARKET_FALLBACK"):
        config.enable_market_fallback = os.getenv("ENABLE_MARKET_FALLBACK", "").lower() in ("true", "1", "yes")
    if os.getenv("MAX_MARKET_LOSS_PERCENT"):
        config.max_market_loss_percent = float(os.getenv("MAX_MARKET_LOSS_PERCENT"))
    if os.getenv("MAX_MARKET_HOLD_MINUTES"):
        config.max_market_hold_minutes = int(os.getenv("MAX_MARKET_HOLD_MINUTES"))
    if os.getenv("MIN_TIME_TO_EXPIRY_MINUTES"):
        config.min_time_to_expiry_minutes = int(os.getenv("MIN_TIME_TO_EXPIRY_MINUTES"))
    if os.getenv("RECOVER_POSITIONS"):
        config.recover_positions_on_start = os.getenv("RECOVER_POSITIONS", "").lower() in ("true", "1", "yes")
    if os.getenv("AGGRESSIVE_PRICING"):
        config.aggressive_pricing = os.getenv("AGGRESSIVE_PRICING", "").lower() in ("true", "1", "yes")
    if os.getenv("SKIP_REBALANCE_IF_PRICE_STABLE"):
        config.skip_rebalance_if_price_stable = os.getenv("SKIP_REBALANCE_IF_PRICE_STABLE", "").lower() in ("true", "1", "yes")
    if os.getenv("PRICE_CHANGE_THRESHOLD"):
        config.price_change_threshold = float(os.getenv("PRICE_CHANGE_THRESHOLD"))
    if os.getenv("STOP_NEW_ORDERS_AFTER_FILL"):
        config.stop_new_orders_after_fill = os.getenv("STOP_NEW_ORDERS_AFTER_FILL", "").lower() in ("true", "1", "yes")
    
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
