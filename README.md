# AlgoMirror - Enterprise Multi-Account OpenAlgo Management Platform

[![Version](https://img.shields.io/badge/version-2.0.0-blue.svg)](https://github.com/openflare/algomirror)
[![License](https://img.shields.io/badge/license-Proprietary-red.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9+-green.svg)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/flask-3.0+-lightgrey.svg)](https://flask.palletsprojects.com/)
[![OpenAlgo](https://img.shields.io/badge/openalgo-compatible-orange.svg)](https://openalgo.in)

> **Enterprise-grade multi-account management platform for OpenAlgo with strategy building, risk management, and real-time analytics**

AlgoMirror is a proprietary, secure, and scalable multi-account management platform developed by **OpenFlare Technologies**. It provides traders with a unified interface to manage multiple OpenAlgo trading accounts across 24 brokers, featuring advanced strategy building, Supertrend-based exits, dynamic margin calculation, and comprehensive risk management.

---

## Table of Contents

- [Key Features](#key-features)
- [What's New in v2.0](#whats-new-in-v20)
- [Prerequisites](#prerequisites)
- [Quick Start Guide](#quick-start-guide)
- [Strategy Builder](#strategy-builder)
- [Risk Management](#risk-management)
- [Margin Calculator](#margin-calculator)
- [Supertrend Indicator](#supertrend-indicator)
- [OpenAlgo Integration](#openalgo-integration)
- [Project Architecture](#project-architecture)
- [Configuration Reference](#configuration-reference)
- [Production Deployment](#production-deployment)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## Key Features

### Multi-Account Management
- Unified dashboard for unlimited trading accounts across 24 brokers
- Primary/secondary account hierarchy with automatic failover
- Real-time synchronization and live updates across all accounts
- Cross-broker support with seamless switching

### Strategy Builder
- Visual strategy construction with multi-leg support
- Instrument support: NIFTY, BANKNIFTY, SENSEX options and futures
- Strike selection: ATM, ITM, OTM with configurable offsets, or premium-based
- Risk profiles: Fixed lots, Conservative (40%), Balanced (65%), Aggressive (80%)
- Entry/exit timing with automatic square-off

### Risk Management
- Max loss and max profit targets with automatic exits
- Trailing stop loss (percentage, points, or amount-based)
- Supertrend-based exits (breakout/breakdown signals)
- Risk event audit logging for compliance
- Position-level and strategy-level P&L tracking

### Dynamic Margin Calculator
- Automatic lot sizing based on available margin
- Trade quality grades: A (95%), B (65%), C (36%) margin utilization
- Expiry vs non-expiry margin awareness
- Freeze quantity handling with automatic order splitting

### Technical Analysis
- Pine Script v6 compatible Supertrend indicator
- Numba-optimized calculations for performance
- Configurable period, multiplier, and timeframe
- Real-time exit signal monitoring

### Enterprise Security
- Zero-trust architecture with no default accounts
- AES-128 Fernet encryption for all API keys
- Multi-tier rate limiting protection
- Comprehensive audit logging
- CSRF protection and Content Security Policy

---

## What's New in v2.0

### Strategy Builder
- Complete visual strategy builder with multi-leg support
- Support for options (CE/PE), futures, and equity instruments
- Multiple strike selection methods including premium-near
- Strategy templates for quick deployment

### Supertrend Integration
- Pine Script v6 compatible implementation using TA-Lib ATR
- Numba JIT compilation for high-performance calculations
- Background exit monitoring service
- Breakout and breakdown exit types

### Margin & Risk Management
- Dynamic lot sizing based on account margin and trade quality
- Configurable margin requirements per instrument
- Trade quality grades (A/B/C) for position sizing
- Risk event logging and audit trail

### Technical Improvements
- Native Python threading (moved from eventlet for Python 3.13+ compatibility)
- Gthread worker for Gunicorn in production
- Background order status polling
- UV package manager support (10-100x faster than pip)

### New Instruments
- SENSEX options and futures support added
- Updated lot sizes and freeze quantities for 2025

---

## Prerequisites

| Requirement | Version | Purpose |
|-------------|---------|---------|
| Python | 3.9+ | Core runtime |
| Node.js | 14+ | CSS build system (Tailwind) |
| OpenAlgo | Latest | Trading platform integration |
| SQLite | Built-in | Development database |
| PostgreSQL | 12+ | Production database (recommended) |
| Redis | 6+ | Session storage & rate limiting (optional) |
| TA-Lib | Latest | Technical analysis library |

### Installing TA-Lib

**Windows:**
Download pre-built binaries from [TA-Lib Windows](https://github.com/cgohlke/talib-build/releases)

**Linux/macOS:**
```bash
# Ubuntu/Debian
sudo apt-get install ta-lib

# macOS
brew install ta-lib
```

---

## Quick Start Guide

### Method 1: Using UV (Recommended - 10-100x Faster)

```bash
# Install UV (if not already installed)
# Windows PowerShell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone repository
git clone https://github.com/openflare/algomirror.git
cd algomirror

# Create and activate virtual environment
uv venv
.venv\Scripts\activate      # Windows
source .venv/bin/activate   # macOS/Linux

# Install dependencies
uv pip install -e .

# Install with development dependencies
uv pip install -e ".[dev]"

# Install Node dependencies and build CSS
npm install
npm run build-css

# Configure environment
cp .env.example .env
# Edit .env with your settings

# Initialize database
python init_db.py

# Run application
python app.py
# Application available at: http://localhost:8000
```

### Method 2: Using pip (Traditional)

```bash
# Clone repository
git clone https://github.com/openflare/algomirror.git
cd algomirror

# Create and activate virtual environment
python -m venv venv
venv\Scripts\activate       # Windows
source venv/bin/activate    # macOS/Linux

# Install dependencies
pip install -r requirements.txt

# Install Node dependencies and build CSS
npm install
npm run build-css

# Configure environment
cp .env.example .env
# Edit .env with your settings

# Initialize database
python init_db.py

# Run application
python app.py
```

### First Login

1. Navigate to `http://localhost:8000`
2. Click "Get Started" to register
3. **First user automatically becomes admin** (zero-trust security)
4. Login and start adding your OpenAlgo accounts

---

## Strategy Builder

### Creating a Strategy

1. Navigate to **Strategy** > **Builder**
2. Configure basic information:
   - Strategy name and description
   - Market condition (Expiry/Non-Expiry/Any)
   - Risk profile (Fixed Lots, Balanced, Conservative, Aggressive)
   - Entry/Exit/Square-off times

3. Add strategy legs:
   - Select instrument (NIFTY, BANKNIFTY, SENSEX)
   - Choose product type (Options, Futures)
   - Configure strike selection (ATM, ITM, OTM with offset)
   - Set action (BUY/SELL) and lots

4. Configure risk management:
   - Max loss and max profit targets
   - Trailing stop loss settings
   - Supertrend exit configuration

5. Select accounts for execution

### Risk Profile Options

| Profile | Margin Usage | Description |
|---------|--------------|-------------|
| Fixed Lots | Manual | Uses explicit lot sizes from strategy legs |
| Conservative | 40% | Lower risk with smaller positions |
| Balanced | 65% | Moderate approach (default) |
| Aggressive | 80% | Higher risk with larger positions |

### Strike Selection Methods

- **ATM**: At-the-money strike
- **ITM**: In-the-money with configurable offset
- **OTM**: Out-of-the-money with configurable offset
- **Strike Price**: Specific strike price value
- **Premium Near**: Strike nearest to specified premium

---

## Risk Management

### Strategy-Level Risk Controls

```
Max Loss Target:
- Set maximum loss threshold for entire strategy
- Automatic exit when threshold breached
- Configurable auto-exit on/off

Max Profit Target:
- Set profit target for strategy
- Automatic exit on target hit
- Lock in profits automatically

Trailing Stop Loss:
- Types: Percentage, Points, Amount
- Activates after position in profit
- Trails as profit increases
```

### Supertrend-Based Exits

Configure Supertrend exits in strategy settings:

| Setting | Default | Description |
|---------|---------|-------------|
| Period | 7 | ATR calculation period |
| Multiplier | 3.0 | Band multiplier |
| Timeframe | 5m | Candle timeframe |
| Exit Type | breakout | Exit on breakout or breakdown |

Exit signals:
- **Breakout**: Exit when price crosses above upper band (bullish)
- **Breakdown**: Exit when price crosses below lower band (bearish)

### Risk Event Logging

All risk threshold breaches are logged:

```
Event Types:
- max_loss: Maximum loss threshold hit
- max_profit: Profit target achieved
- trailing_sl: Trailing stop loss triggered
- supertrend: Supertrend exit signal

Logged Information:
- Strategy and execution IDs
- Threshold and current values
- Action taken (close_all, close_partial, alert_only)
- Exit order IDs
- Timestamp
```

---

## Margin Calculator

### How It Works

1. **Get Available Margin**: Fetches from account via OpenAlgo API
2. **Apply Trade Quality**: Multiplies by grade percentage (A=95%, B=65%, C=36%)
3. **Get Margin Requirement**: Based on instrument and expiry/non-expiry
4. **Calculate Lots**: Usable margin / margin per lot
5. **Apply Freeze Limit**: Cap at max_lots_per_order from settings

### Trade Quality Grades

| Grade | Margin % | Risk Level | Description |
|-------|----------|------------|-------------|
| A | 95% | Conservative | Maximum margin utilization |
| B | 65% | Moderate | Balanced approach |
| C | 36% | Aggressive | Lower capital deployment |

### Default Margin Requirements (per lot)

**NIFTY/BANKNIFTY:**
| Trade Type | Expiry | Non-Expiry |
|------------|--------|------------|
| CE/PE Sell | 205,000 | 250,000 |
| CE & PE Sell | 250,000 | 320,000 |
| Futures | 215,000 | 215,000 |

**SENSEX:**
| Trade Type | Expiry | Non-Expiry |
|------------|--------|------------|
| CE/PE Sell | 180,000 | 220,000 |
| CE & PE Sell | 225,000 | 290,000 |
| Futures | 185,000 | 185,000 |

### Trading Settings (Lot Sizes as of 2025)

| Symbol | Lot Size | Freeze Qty | Max Lots/Order |
|--------|----------|------------|----------------|
| NIFTY | 75 | 1,800 | 24 |
| BANKNIFTY | 35 | 900 | 25 |
| SENSEX | 20 | 1,000 | 50 |

---

## Supertrend Indicator

### Implementation Details

AlgoMirror uses a Pine Script v6 compatible Supertrend implementation:

```python
# Key characteristics:
- Uses TA-Lib ATR (RMA-based, matching Pine Script ta.atr)
- Numba JIT compilation for performance
- Handles NaN values from ATR warmup period
- Direction: 1 = Bullish (use lower band), -1 = Bearish (use upper band)
```

### Calculation Formula

```
ATR = ta.atr(period)  # TA-Lib RMA-based ATR
HL2 = (High + Low) / 2

Basic Upper Band = HL2 + (Multiplier * ATR)
Basic Lower Band = HL2 - (Multiplier * ATR)

Final Bands adjusted based on previous close and bands
Direction changes when close crosses bands
```

### Background Exit Service

The Supertrend Exit Service runs as a daemon thread:

1. Monitors active strategies with Supertrend exits enabled
2. Fetches OHLC data at configured intervals
3. Calculates Supertrend and checks for direction changes
4. Triggers automatic exits on signal
5. Logs risk events for audit trail

---

## OpenAlgo Integration

### Supported Brokers (24)

- 5paisa & 5paisa (XTS)
- Aliceblue
- AngelOne
- Compositedge (XTS)
- Definedge
- Dhan
- Firstock
- Flattrade
- Fyers
- Groww
- IIFL (XTS)
- IndiaBulls
- IndMoney
- Kotak Securities
- Motilal Oswal
- Paytm
- Pocketful
- Shoonya
- Tradejini
- Upstox
- Wisdom Capital (XTS)
- Zebu
- Zerodha

### Extended OpenAlgo Client

```python
from app.utils.openalgo_client import ExtendedOpenAlgoAPI

client = ExtendedOpenAlgoAPI(
    api_key='your_api_key',
    host='http://127.0.0.1:5000'
)

# Test connection (AlgoMirror extension)
ping_response = client.ping()

# Standard OpenAlgo operations
funds = client.funds()
positions = client.positionbook()
orders = client.orderbook()
holdings = client.holdings()
```

### Connection Testing

```bash
curl -X POST http://127.0.0.1:5000/api/v1/ping \
  -H "Content-Type: application/json" \
  -d '{"apikey":"your_api_key"}'

# Expected response
{
  "status": "success",
  "data": {
    "broker": "zerodha",
    "message": "pong"
  }
}
```

---

## Project Architecture

### Directory Structure

```
algomirror/
├── app/                              # Main application package
│   ├── __init__.py                   # Flask app factory
│   ├── models.py                     # SQLAlchemy models
│   ├── auth/                         # Authentication blueprint
│   ├── main/                         # Dashboard and landing pages
│   ├── accounts/                     # Account management
│   ├── trading/                      # Trading operations
│   ├── strategy/                     # Strategy builder and execution
│   ├── margin/                       # Margin management
│   ├── api/                          # REST API endpoints
│   ├── utils/                        # Utility modules
│   │   ├── openalgo_client.py        # Extended OpenAlgo client
│   │   ├── websocket_manager.py      # WebSocket manager
│   │   ├── supertrend.py             # Numba-optimized Supertrend
│   │   ├── supertrend_exit_service.py # Background exit monitoring
│   │   ├── margin_calculator.py      # Dynamic lot sizing
│   │   ├── strategy_executor.py      # Parallel execution engine
│   │   ├── order_status_poller.py    # Background order polling
│   │   └── rate_limiter.py           # Rate limiting decorators
│   ├── templates/                    # Jinja2 HTML templates
│   └── static/                       # CSS, JS, images
├── migrations/                       # Database migrations
├── docs/                             # Documentation
├── logs/                             # Application logs
├── instance/                         # Instance-specific files
├── config.py                         # Configuration
├── app.py                            # Entry point
├── init_db.py                        # Database initialization
├── requirements.txt                  # Python dependencies
├── pyproject.toml                    # UV/pip project config
└── package.json                      # Node dependencies
```

### Database Models

**Core Models:**
- User - Authentication and authorization
- TradingAccount - OpenAlgo connections with encrypted API keys
- ActivityLog - Audit trail

**Strategy Models:**
- Strategy - Strategy configuration and settings
- StrategyLeg - Individual legs with instrument details
- StrategyExecution - Execution tracking and P&L

**Margin & Risk Models:**
- MarginRequirement - Instrument margin settings
- TradeQuality - A/B/C grade configurations
- MarginTracker - Real-time margin tracking
- RiskEvent - Risk threshold breach audit log
- TradingSettings - Lot sizes and freeze quantities

**Configuration Models:**
- TradingHoursTemplate - Market hours configuration
- TradingSession - Day-wise trading sessions
- MarketHoliday - Holiday calendar
- WebSocketSession - Active WebSocket sessions

### Threading Architecture

```
Main Application (Flask/Gunicorn gthread worker)
└── HTTP Request Handlers

Background Daemon Threads:
├── WebSocket Manager Thread
│   └── Connection monitoring & reconnection
├── Supertrend Exit Service Thread
│   └── Price monitoring for indicator-based exits
├── Order Status Poller Thread
│   └── Periodic order status synchronization
└── Risk Monitor Threads (per strategy)
    └── P&L threshold monitoring

ThreadPoolExecutor (Strategy Execution):
└── Parallel order placement across accounts

Note: Uses native Python threading (not eventlet)
Compatible with Python 3.13+ and TA-Lib
```

---

## Configuration Reference

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| SECRET_KEY | Yes | dev-key | Flask session encryption |
| DATABASE_URL | No | sqlite:///algomirror.db | Database connection |
| FLASK_ENV | No | development | Environment mode |
| LOG_LEVEL | No | INFO | Logging verbosity |
| REDIS_URL | No | memory:// | Redis for caching |
| ENCRYPTION_KEY | No | Auto-generated | Fernet encryption key |
| SESSION_TYPE | No | filesystem | Session storage type |

### Rate Limiting

| Endpoint | Limit | Purpose |
|----------|-------|---------|
| Global | 1000/minute | Overall IP protection |
| Authentication | 10/minute | Login/register/password |
| API Data | 100/minute | Trading data retrieval |
| Heavy Operations | 20/minute | Connection tests/refresh |

### Password Policy

- Minimum 8 characters
- At least one uppercase letter (A-Z)
- At least one lowercase letter (a-z)
- At least one digit (0-9)
- At least one special character (!@#$%^&*()_+-=[]{}|;:,.<>?)
- Cannot be common passwords

---

## Production Deployment

### Requirements

- PostgreSQL database
- Redis for caching (optional)
- Nginx/Apache reverse proxy
- SSL certificate (Let's Encrypt)
- Gunicorn with gthread worker

### Production Configuration

```env
SECRET_KEY=randomly-generated-256-bit-key
DATABASE_URL=postgresql://user:password@localhost/algomirror_prod
REDIS_URL=redis://localhost:6379/0
FLASK_ENV=production
SESSION_TYPE=sqlalchemy
LOG_LEVEL=WARNING
```

### Gunicorn Configuration

```bash
gunicorn -w 4 -k gthread --threads 2 -b 0.0.0.0:8000 app:app
```

### Nginx Configuration

```nginx
server {
    listen 443 ssl http2;
    server_name algomirror.yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/algomirror.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/algomirror.yourdomain.com/privkey.pem;

    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Frame-Options "DENY" always;
    add_header X-Content-Type-Options "nosniff" always;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

---

## Troubleshooting

### Common Issues

**Connection Issues:**
```bash
# Verify OpenAlgo is running
curl http://127.0.0.1:5000
curl -X POST http://127.0.0.1:5000/api/v1/ping -d '{"apikey":"test"}'
```

**Database Issues:**
```bash
python init_db.py reset  # Warning: Deletes all data
python init_db.py
flask db upgrade
```

**CSS/Styling Issues:**
```bash
npm run build-css
# Check app/static/css/compiled.css exists
```

**TA-Lib Import Errors:**
```bash
# Ensure TA-Lib C library is installed
# Windows: Download from GitHub releases
# Linux: sudo apt-get install ta-lib
# macOS: brew install ta-lib
```

### Debug Checklist

1. Virtual environment activated
2. All dependencies installed
3. CSS compiled (`npm run build-css`)
4. Database initialized (`python init_db.py`)
5. OpenAlgo server running
6. Valid API key configured
7. No errors in `logs/algomirror.log`

---

## Database Management

```bash
# Initialize fresh database
python init_db.py

# Reset database (deletes all data)
python init_db.py reset

# Create test data (development only)
python init_db.py testdata

# Create migration after model changes
flask db migrate -m "Description"

# Apply migrations
flask db upgrade
```

---

## Version History

### v2.0.0 (Current)
- Strategy builder with multi-leg support
- Supertrend indicator (Pine Script v6 compatible)
- Risk management (max loss/profit, trailing SL, Supertrend exits)
- Dynamic margin calculator with trade quality grades
- Parallel strategy execution with ThreadPoolExecutor
- Background services (Supertrend exits, order polling)
- Native Python threading (gthread worker)
- SENSEX instrument support
- UV package manager support
- Risk event audit logging

### v1.0.0
- Multi-account OpenAlgo integration (24 brokers)
- Zero-trust security architecture
- Fernet encryption for API keys
- Multi-tier rate limiting
- Real-time dashboard
- Mobile-responsive UI with OpenAlgo theme

---

## License

**Proprietary Software - Copyright 2024 OpenFlare Technologies. All Rights Reserved.**

This software is confidential and proprietary. Unauthorized copying, modification, or distribution is prohibited.

---

## Support

- **Documentation**: See `docs/` folder for detailed guides
- **GitHub Issues**: Report bugs and feature requests
- **Email**: support@openflare.tech

---

**Built by OpenFlare Technologies | Powered by OpenAlgo**
