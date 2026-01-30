# 🎯 Predict.fun Market Maker Bot

**Liquidity Provider Bot для фарма Predict Points на BNB Chain**

[![Python](https://img.shields.io/badge/Python-3.9+-blue.svg)](https://python.org)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

---

## 📋 Содержание / Table of Contents

1. [Введение / Introduction](#1-введение--introduction)
2. [Требования и установка / Requirements & Installation](#2-требования-и-установка--requirements--installation)
3. [Конфигурация / Configuration](#3-конфигурация--configuration)
4. [Использование / Usage](#4-использование--usage)
5. [Архитектура скрипта / Script Architecture](#5-архитектура-скрипта--script-architecture)
6. [Возможные улучшения / Possible Improvements](#6-возможные-улучшения--possible-improvements)
7. [Риски и Disclaimers / Risks & Disclaimers](#7-риски-и-disclaimers--risks--disclaimers)

---

## 1. Введение / Introduction

### Что делает этот скрипт / What This Script Does

Этот бот реализует стратегию **предоставления ликвидности** (market making) на платформе [Predict.fun](https://predict.fun) — предсказательном рынке на BNB Chain.

**Основная цель:** Заработать **Predict Points** за счёт:
- Размещения лимит-ордеров с обеих сторон рынка (YES и NO)
- **2x мультипликатор** за ликвидность в "uncertain markets" (вероятность 40-60%)
- Автоматической ребалансировки позиций при изменении цен

### Как работает фарм поинтов / How Points Farming Works

```
┌─────────────────────────────────────────────────────────────┐
│                    PREDICT POINTS SYSTEM                     │
├─────────────────────────────────────────────────────────────┤
│  Base Points        = Order Size × Time in Orderbook        │
│  Uncertain Market   = 2x multiplier (prob 40-60%)           │
│  Low OI Bonus       = Higher points for less liquid markets │
└─────────────────────────────────────────────────────────────┘
```

### Delta-Neutral Стратегия / Delta-Neutral Strategy

```
    YES Market                    NO Market
   ┌──────────┐                  ┌──────────┐
   │ BUY  YES │ ←── Mid Price ──→│ BUY  NO  │
   │ @ 0.47   │      0.50        │ @ 0.47   │
   │ $25      │                  │ $25      │
   └──────────┘                  └──────────┘
         │                              │
         └──────────────┬───────────────┘
                        ▼
              Delta-Neutral Position
              (≈0 directional risk)
```

---

## ⚠️ ВАЖНЫЕ ПРЕДУПРЕЖДЕНИЯ / WARNINGS

> **🚫 ЭТО НЕ ФИНАНСОВАЯ РЕКОМЕНДАЦИЯ / NOT FINANCIAL ADVICE**

| Risk | Description |
|------|-------------|
| 💸 **Execution Risk** | Ордера могут исполниться в убыток при резком движении цены |
| ⛽ **Gas Costs** | Газ на BNB Chain может съедать прибыль на малых объёмах |
| 📜 **Platform Rules** | Правила Predict.fun могут измениться, боты могут быть запрещены |
| 🤖 **Anti-Sybil** | Не используйте множество аккаунтов — это может привести к бану |
| ⏰ **Market Resolution** | Рынки закрываются — следите за датами |

**ТЕСТИРУЙТЕ НА МАЛЫХ СУММАХ ($100-500) ПЕРЕД МАСШТАБИРОВАНИЕМ!**

---

## 2. Требования и установка / Requirements & Installation

### Системные требования / System Requirements

- Python 3.9+
- Доступ к интернету
- BNB для газа на кошельке
- USDT на Predict Account

### Установка / Installation

```bash
# 1. Клонируйте репозиторий или скопируйте файлы
git clone <repo-url>
cd predict-market-maker

# 2. Создайте виртуальное окружение (рекомендуется)
python -m venv venv
source venv/bin/activate  # Linux/Mac
# или: venv\Scripts\activate  # Windows

# 3. Установите зависимости
pip install -r requirements.txt

# Или установите вручную:
pip install predict-sdk python-dotenv web3
```

### Получение Predict Account и Private Key

1. **Зайдите на** [predict.fun](https://predict.fun)
2. **Создайте аккаунт** (если ещё нет)
3. **Перейдите в настройки:** `Account → Settings`
4. **Найдите Predict Account Address** — это ваш smart wallet
5. **Экспортируйте Privy Wallet Private Key:**
   - `Settings → Export Wallet → Reveal Private Key`
   - ⚠️ **НИКОМУ НЕ ПОКАЗЫВАЙТЕ ЭТОТ КЛЮЧ!**

```
┌────────────────────────────────────────────────────┐
│           predict.fun/account/settings             │
├────────────────────────────────────────────────────┤
│  Predict Account: 0x1234...abcd                   │
│                                                    │
│  [Export Wallet]  →  [Reveal Private Key]         │
│                       0xabc123...                  │
└────────────────────────────────────────────────────┘
```

---

## 3. Конфигурация / Configuration

### Создание .env файла / Creating .env File

```bash
# Скопируйте пример конфигурации
cp .env.example .env

# Отредактируйте файл
nano .env  # или любой другой редактор
```

### Основные параметры / Main Parameters

| Параметр | По умолчанию | Описание |
|----------|--------------|----------|
| `PRIVATE_KEY` | — | Приватный ключ кошелька (обязательно) |
| `MAX_OI_USD` | 15000 | Макс. Open Interest для рынка ($) |
| `TARGET_SPREAD` | 0.03 | Спред от mid-price (3%) |
| `ORDER_SIZE_USD` | 25 | Размер каждого ордера ($) |
| `REBALANCE_INTERVAL_SEC` | 300 | Интервал ребалансировки (сек) |
| `MAX_TOTAL_EXPOSURE_USD` | 500 | Макс. общая позиция ($) |
| `MARKET_IDS` | — | Конкретные рынки (через запятую) |
| `LOG_LEVEL` | INFO | Уровень логирования |

### Пример конфигурации для фарма / Example Config for Farming

```env
# Агрессивный фарм на низколиквидных рынках
PRIVATE_KEY=0x...
MAX_OI_USD=10000
TARGET_SPREAD=0.02
ORDER_SIZE_USD=50
REBALANCE_INTERVAL_SEC=180
MAX_TOTAL_EXPOSURE_USD=1000
LOG_LEVEL=INFO
```

---

## 4. Использование / Usage

### Запуск бота / Running the Bot

```bash
# Активируйте виртуальное окружение (если используете)
source venv/bin/activate

# Запустите бота
python market_maker.py
```

### Пример вывода / Example Output

```
============================================================
🎯 PREDICT.FUN MARKET MAKER BOT
============================================================

⚠️  ВАЖНО / WARNING:
   - Это НЕ финансовая рекомендация / NOT financial advice
   - Тестируйте на малых суммах / Test with small amounts
   - Читайте правила платформы / Read platform rules

2026-01-30 15:30:00 | INFO     | ✅ MarketMakerBot initialized successfully
2026-01-30 15:30:00 | INFO     | 🔑 Wallet: 0x1234...abcd
2026-01-30 15:30:00 | INFO     | 💰 USDT Balance: $250.00
2026-01-30 15:30:01 | INFO     | 🔍 Searching for suitable markets...
2026-01-30 15:30:02 | INFO     |   ✅ Will BTC reach $100k by Feb? | OI: $5,230 | Prob: 52%
2026-01-30 15:30:03 | INFO     |   ✅ ETH ETF approval by Q1? | OI: $8,100 | Prob: 45%
2026-01-30 15:30:04 | INFO     | 📊 Found 2 suitable markets
2026-01-30 15:30:05 | INFO     | 📝 Placing orders for: Will BTC reach $100k...
2026-01-30 15:30:05 | INFO     |   📊 Mid: 0.5200 | Bid: 0.4900 | Ask: 0.5500
2026-01-30 15:30:06 | INFO     |   ✅ YES BUY order placed: 0.4900 x 51.02
2026-01-30 15:30:07 | INFO     |   ✅ NO BUY order placed: 0.4500 x 55.56
```

### Остановка бота / Stopping the Bot

```bash
# Graceful shutdown (Ctrl+C)
^C
2026-01-30 16:45:00 | INFO     | ⏹️  Received shutdown signal...
2026-01-30 16:45:00 | INFO     | 🛑 Initiating graceful shutdown...
2026-01-30 16:45:01 | INFO     | 🗑️  Cancelled 4 orders on shutdown
2026-01-30 16:45:01 | INFO     | 👋 Bot shutdown complete. Goodbye!
```

---

## 5. Архитектура скрипта / Script Architecture

### Структура класса MarketMakerBot

```
MarketMakerBot
├── __init__()              # Инициализация SDK и signer
├── get_suitable_markets()  # Фильтр подходящих рынков
├── calculate_order_params()# Расчёт bid/ask цен и размеров
├── place_limit_orders()    # Размещение ордеров YES + NO
├── cancel_old_orders()     # Отмена устаревших ордеров
├── check_and_update_orders() # Проверка статуса ордеров
├── should_rebalance()      # Нужна ли ребалансировка?
├── monitor_and_rebalance() # Основной цикл (asyncio)
├── check_balance()         # Проверка баланса USDT
├── log_statistics()        # Логирование статистики
├── run()                   # Запуск бота
└── shutdown()              # Graceful shutdown
```

### Диаграмма работы / Flow Diagram

```
                    ┌─────────────────┐
                    │   START BOT     │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │  Check Balance  │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │  Find Markets   │
                    │  (OI < $15k,    │
                    │   prob 40-60%)  │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │ Place Initial   │
                    │ Orders (YES+NO) │
                    └────────┬────────┘
                             │
              ┌──────────────▼──────────────┐
              │      MONITORING LOOP        │
              │  (every 5 min or on Δprice) │
              └──────────────┬──────────────┘
                             │
         ┌───────────────────┼───────────────────┐
         │                   │                   │
┌────────▼────────┐ ┌────────▼────────┐ ┌────────▼────────┐
│ Check Order     │ │ Need to         │ │ Log Statistics  │
│ Status (filled?)│ │ Rebalance?      │ │ (PnL, points)   │
└────────┬────────┘ └────────┬────────┘ └─────────────────┘
         │                   │
         │          ┌────────▼────────┐
         │          │ Cancel Old      │
         │          │ Place New       │
         │          └────────┬────────┘
         │                   │
         └───────────────────┼───────────────────┘
                             │
                    ┌────────▼────────┐
                    │   Continue or   │
                    │   Shutdown      │
                    └─────────────────┘
```

---

## 6. Возможные улучшения / Possible Improvements

### 📱 Telegram уведомления

```python
# Добавить в requirements.txt:
# python-telegram-bot>=20.0

# Пример интеграции:
import telegram

async def send_telegram_alert(bot_token: str, chat_id: str, message: str):
    bot = telegram.Bot(token=bot_token)
    await bot.send_message(chat_id=chat_id, text=message)

# Использование:
await send_telegram_alert(
    os.getenv("TG_BOT_TOKEN"),
    os.getenv("TG_CHAT_ID"),
    f"💰 Order filled: YES @ 0.52 for $25"
)
```

### 📊 Расчёт ожидаемых поинтов

```python
def estimate_points(
    order_size_usd: float,
    time_in_orderbook_hours: float,
    is_uncertain_market: bool = True
) -> float:
    """
    Примерный расчёт поинтов за ликвидность
    (формула может отличаться от реальной!)
    """
    base_points = order_size_usd * time_in_orderbook_hours
    multiplier = 2.0 if is_uncertain_market else 1.0
    return base_points * multiplier
```

### 🎯 Авто-выбор лучших рынков

```python
async def score_market(market) -> float:
    """
    Оценка рынка для приоритизации
    Higher score = better market for farming
    """
    score = 0.0
    
    # Низкий OI = больше поинтов
    if market.open_interest_usd < 5000:
        score += 30
    elif market.open_interest_usd < 10000:
        score += 20
    
    # Uncertain probability (40-60%) = 2x multiplier
    prob = market.yes_price
    if 0.45 <= prob <= 0.55:
        score += 50
    elif 0.40 <= prob <= 0.60:
        score += 30
    
    # Новые рынки
    if market.created_at > (datetime.now() - timedelta(days=7)):
        score += 20
    
    return score
```

### 💰 Yield-Bearing Collateral

```python
# Если Predict.fun поддерживает yield-bearing collateral (например, stUSDT):
# Проверьте документацию на dev.predict.fun

async def check_yield_options(self):
    """Check if yield-bearing collateral is enabled"""
    try:
        account_info = await self.client.get_account()
        if account_info.yield_bearing_enabled:
            self.logger.info("✅ Yield-bearing collateral is enabled!")
            self.logger.info(f"   Current APY: {account_info.yield_apy}%")
    except:
        pass
```

---

## 7. Риски и Disclaimers / Risks & Disclaimers

### ⚠️ Финансовые риски / Financial Risks

| Risk | Mitigation |
|------|------------|
| **Execution in loss** | Тестируйте на малых суммах, используйте tight spreads |
| **Gas costs** | BNB Chain дешёвый, но следите за ценой газа |
| **Market resolution** | Избегайте рынков, закрывающихся скоро |
| **Smart contract risk** | Используйте только официальный SDK |

### 📜 Регуляторные риски / Regulatory Risks

- Правила Predict.fun могут запретить автоматическую торговлю
- Проверяйте [Terms of Service](https://predict.fun/terms) перед использованием
- Prediction markets могут быть ограничены в вашей юрисдикции

### 🔐 Безопасность / Security

- **НИКОГДА** не хардкодьте приватные ключи
- **НИКОГДА** не коммитьте `.env` файлы в git
- Используйте отдельный кошелёк для бота
- Храните только необходимый баланс на торговом кошельке

### 📚 Официальная документация / Official Documentation

- **API Docs:** https://dev.predict.fun/
- **Python SDK:** https://github.com/PredictDotFun/sdk-python
- **TypeScript SDK:** https://github.com/PredictDotFun/sdk

---

## 📝 License

MIT License — use at your own risk.

---

## 🙏 Disclaimer

**ЭТО ПРОГРАММНОЕ ОБЕСПЕЧЕНИЕ ПРЕДОСТАВЛЯЕТСЯ "КАК ЕСТЬ" БЕЗ КАКИХ-ЛИБО ГАРАНТИЙ.**

Автор не несёт ответственности за любые финансовые потери, понесённые в результате использования этого скрипта. Вы используете его на свой страх и риск.

**THIS SOFTWARE IS PROVIDED "AS IS" WITHOUT ANY WARRANTIES.**

The author is not responsible for any financial losses incurred as a result of using this script. You use it at your own risk.
