# Option Chain Module - Technical Architecture Document

## Overview

The Option Chain Module is designed to provide real-time option chain data for NIFTY and BANKNIFTY indices using OpenAlgo Python SDK with WebSocket integration. This module serves as the foundation for advanced options trading strategies and premium-based stop-loss calculations.

**Critical Feature**: The option chain automatically starts monitoring both NIFTY and BANKNIFTY when the primary trading account is connected. This ensures continuous real-time data availability for stop-loss monitoring, target tracking, and strategy execution - regardless of whether the user is viewing the option chain page.

## Core Requirements

### 1. Underlying Assets
- **NIFTY** (Exchange: NFO, Lot Size: 75)
- **BANKNIFTY** (Exchange: NFO, Lot Size: 35)

### 2. Strike Range Coverage
- **ATM (At The Money)**: Current underlying LTP rounded to nearest strike
- **ITM (In The Money)**: +20 strikes from ATM for both CE and PE
- **OTM (Out The Money)**: -20 strikes from ATM for both CE and PE
- **Total Coverage**: 41 strikes per underlying (ATM + 20 ITM + 20 OTM)

### 3. Option Chain Tagging System
```
ITM20, ITM19, ITM18, ..., ITM3, ITM2, ITM1, ATM, OTM1, OTM2, OTM3, ..., OTM18, OTM19, OTM20
```

## Technical Architecture

### 1. Data Flow Pipeline

```
OpenAlgo API → Strike Discovery → WebSocket Subscription → Real-time Updates → Option Chain Display
```

#### Step 1: Underlying LTP & ATM Calculation
- Fetch current LTP for NIFTY/BANKNIFTY using `client.quotes()`
- Calculate ATM strike (round to nearest 50 for NIFTY, 100 for BANKNIFTY)
- Generate strike range: [ATM-20*step, ATM-19*step, ..., ATM, ..., ATM+19*step, ATM+20*step]

#### Step 2: Expiry Selection
- Use `client.expiry()` to get available expiry dates
- Select current/near-month expiry for real-time trading
- Support multiple expiry selection for advanced strategies

#### Step 3: Symbol Construction
- Follow OpenAlgo symbol format: `[Base Symbol][Expiration Date][Strike Price][Option Type]`
- Example: `NIFTY17JUL2524500CE`, `BANKNIFTY17JUL2550000PE`
- Generate symbol list for all strikes and both CE/PE options

#### Step 4: WebSocket Integration with Market Depth
- Initialize OpenAlgo WebSocket client with multiple subscription modes
- **Quote Mode**: For underlying index real-time LTP
- **Depth Mode**: For option strikes to get bid/ask and market depth
- Handle connection management and reconnection logic
- Process incoming data and update option chain with depth information

### 2. Data Structure

#### Enhanced Option Chain Data Model with Market Depth
```python
{
    "underlying": "NIFTY",
    "underlying_ltp": 24500.75,
    "underlying_bid": 24500.50,
    "underlying_ask": 24501.00,
    "atm_strike": 24500,
    "expiry": "17-JUL-25",
    "timestamp": "2025-07-17T10:30:00",
    "options": [
        {
            "tag": "ITM20",
            "strike": 23500,
            "ce_data": {
                "symbol": "NIFTY17JUL2523500CE",
                "ltp": 1250.50,
                "bid": 1250.25,
                "ask": 1250.75,
                "bid_qty": 150,
                "ask_qty": 225,
                "spread": 0.50,
                "volume": 45678,
                "oi": 234567
            },
            "pe_data": {
                "symbol": "NIFTY17JUL2523500PE", 
                "ltp": 45.25,
                "bid": 45.00,
                "ask": 45.50,
                "bid_qty": 375,
                "ask_qty": 450,
                "spread": 0.50,
                "volume": 12345,
                "oi": 98765
            }
        },
        {
            "tag": "ATM",
            "strike": 24500,
            "ce_data": {
                "symbol": "NIFTY17JUL2524500CE",
                "ltp": 125.75,
                "bid": 125.50,
                "ask": 126.00,
                "bid_qty": 750,
                "ask_qty": 600,
                "spread": 0.50,
                "volume": 234567,
                "oi": 1234567
            },
            "pe_data": {
                "symbol": "NIFTY17JUL2524500PE",
                "ltp": 125.25,
                "bid": 125.00,
                "ask": 125.50,
                "bid_qty": 825,
                "ask_qty": 900,
                "spread": 0.50,
                "volume": 198765,
                "oi": 987654
            }
        }
        // ... more strikes with complete depth data
    ],
    "market_metrics": {
        "total_ce_volume": 5678901,
        "total_pe_volume": 4567890,
        "pcr": 0.80,
        "max_pain": 24550,
        "iv_skew": 1.15
    }
}
```

### 3. Module Components

#### 3.1 Enhanced Option Chain Class with Depth Support (`app/utils/option_chain.py`)
```python
class OptionChainManager:
    """
    Singleton class managing option chain with market depth
    Handles both LTP and bid/ask data for order management
    """
    
    def __init__(self, underlying, expiry):
        self.underlying = underlying
        self.expiry = expiry
        self.strike_step = 50 if underlying == 'NIFTY' else 100
        self.option_data = {}
        self.subscription_map = {}
        
    def initialize(self, underlying, expiry):
        """Setup option chain with depth subscriptions"""
        self.calculate_atm()
        self.generate_strikes()
        self.setup_depth_subscriptions()
        
    def setup_depth_subscriptions(self):
        """
        Configure WebSocket subscriptions with appropriate modes
        - Quote mode for underlying (NIFTY/BANKNIFTY spot)
        - Depth mode for all option strikes (CE & PE)
        """
        # Subscribe to underlying in quote mode
        self.subscribe_underlying_quote()
        
        # Subscribe to options in depth mode for bid/ask
        for strike_data in self.option_data.values():
            ce_symbol = strike_data['ce_symbol']
            pe_symbol = strike_data['pe_symbol']
            
            # Depth subscription for market data
            self.subscribe_option_depth(ce_symbol)
            self.subscribe_option_depth(pe_symbol)
    
    def subscribe_option_depth(self, symbol):
        """Subscribe to option symbol in depth mode"""
        subscription = {
            'symbol': symbol,
            'exchange': 'NFO',
            'mode': 'depth'  # Get full depth including bid/ask
        }
        self.websocket_manager.subscribe(subscription)
        
    def handle_depth_update(self, data):
        """
        Process incoming depth data for options
        Extract top-level bid/ask for order management
        """
        symbol = data['symbol']
        
        if symbol in self.subscription_map:
            strike_info = self.subscription_map[symbol]
            option_type = strike_info['type']  # 'CE' or 'PE'
            
            # Update with depth data
            depth_data = {
                'ltp': data.get('ltp', 0),
                'bid': data.get('bids', [{}])[0].get('price', 0),  # Top bid
                'ask': data.get('asks', [{}])[0].get('price', 0),  # Top ask
                'bid_qty': data.get('bids', [{}])[0].get('quantity', 0),
                'ask_qty': data.get('asks', [{}])[0].get('quantity', 0),
                'spread': 0,  # Calculate spread
                'volume': data.get('volume', 0),
                'oi': data.get('oi', 0)
            }
            
            # Calculate spread
            if depth_data['bid'] > 0 and depth_data['ask'] > 0:
                depth_data['spread'] = depth_data['ask'] - depth_data['bid']
            
            # Update option chain data
            self.update_option_depth(strike_info['strike'], option_type, depth_data)
    
    def get_execution_price(self, symbol, action, quantity):
        """
        Calculate expected execution price based on market depth
        Used for order management and slippage calculation
        """
        if action == 'BUY':
            # For buy orders, use ask price
            return self.get_option_ask(symbol)
        else:
            # For sell orders, use bid price
            return self.get_option_bid(symbol)
    
    def get_option_spread(self, symbol):
        """Get bid-ask spread for a symbol"""
        if symbol in self.subscription_map:
            data = self.get_option_data(symbol)
            return data.get('spread', 0)
        return 0
```

#### 3.2 WebSocket Manager (`app/utils/websocket_manager.py`)
- Handle OpenAlgo WebSocket connections
- Manage subscription/unsubscription
- Connection health monitoring and reconnection
- Data parsing and distribution
- Background thread for continuous monitoring

#### 3.3 Background Service Integration (`app/utils/background_services.py`)
- **Auto-start on primary account connection**: Monitors account status and starts option chains
- **Continuous monitoring**: Runs in background thread pool
- **Health checks**: Ensures WebSocket connections remain active
- **Data persistence**: Maintains option chain data in memory for instant access

#### 3.4 Flask Routes (`app/trading/routes.py`)
- Route: `/option-chain` - Display option chain interface
- Route: `/api/option-chain/<underlying>` - JSON API for real-time data
- Route: `/api/option-chain/status` - Check monitoring status
- Support query parameters: `underlying`, `expiry`
- Server-Sent Events (SSE) endpoint for live updates

### 4. Strike Calculation Logic

#### NIFTY Strike Steps
- Strike interval: 50 points
- ATM calculation: `round(underlying_ltp / 50) * 50`
- Example: LTP 24,567 → ATM 24,550

#### BANKNIFTY Strike Steps  
- Strike interval: 100 points
- ATM calculation: `round(underlying_ltp / 100) * 100`
- Example: LTP 52,834 → ATM 52,800

### 5. Trading Hours Management & Scheduling

#### Trading Hours Template System
A comprehensive trading hours configuration system that controls when WebSocket connections are active, ensuring efficient resource utilization and compliance with market timings.

##### Template Configuration Model
```python
class TradingHoursTemplate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    timezone = db.Column(db.String(50), default='Asia/Kolkata')
    is_active = db.Column(db.Boolean, default=True)
    
    # Session timings
    sessions = db.relationship('TradingSession', backref='template', lazy='dynamic')
    holidays = db.relationship('MarketHoliday', backref='template', lazy='dynamic')
    
class TradingSession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(db.Integer, db.ForeignKey('trading_hours_template.id'))
    day_of_week = db.Column(db.Integer)  # 0=Monday, 6=Sunday
    
    # Pre-market session
    pre_market_start = db.Column(db.Time)
    pre_market_end = db.Column(db.Time)
    
    # Regular market session
    market_open = db.Column(db.Time)
    market_close = db.Column(db.Time)
    
    # Post-market session
    post_market_start = db.Column(db.Time)
    post_market_end = db.Column(db.Time)
    
    is_trading_day = db.Column(db.Boolean, default=True)
    
class MarketHoliday(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(db.Integer, db.ForeignKey('trading_hours_template.id'))
    date = db.Column(db.Date)
    description = db.Column(db.String(200))
    holiday_type = db.Column(db.String(50))  # 'full_day', 'muhurat', 'special'
```

##### Default NSE Trading Hours Configuration
```python
NSE_DEFAULT_TEMPLATE = {
    "name": "NSE Equity & F&O",
    "timezone": "Asia/Kolkata",
    "sessions": {
        "monday_to_friday": {
            "pre_market": {"start": "09:00", "end": "09:08"},
            "normal_market": {"start": "09:15", "end": "15:30"},
            "post_market": {"start": "15:40", "end": "16:00"},
            "eod_processing": "15:30"
        },
        "saturday_sunday": {
            "is_trading_day": False
        }
    },
    "special_sessions": {
        "muhurat_trading": {
            "enabled": True,
            "typical_duration": "75 minutes"
        },
        "expiry_day_extension": {
            "monthly_expiry": {"extend_by": "0 minutes"},
            "weekly_expiry": {"extend_by": "0 minutes"}
        }
    }
}
```

#### Intelligent WebSocket Scheduler
```python
class WebSocketScheduler:
    def __init__(self, trading_template):
        self.template = trading_template
        self.scheduler = BackgroundScheduler(timezone='Asia/Kolkata')
        self.active_connections = {}
        self.subscription_queue = deque()
        
    def initialize(self):
        """Setup scheduled tasks based on trading hours"""
        for session in self.template.sessions:
            if session.is_trading_day:
                # Schedule pre-market connection
                self.scheduler.add_job(
                    func=self.start_pre_market_connection,
                    trigger='cron',
                    day_of_week=session.day_of_week,
                    hour=session.pre_market_start.hour,
                    minute=session.pre_market_start.minute - 5,  # Connect 5 mins early
                    id=f'pre_market_{session.day_of_week}'
                )
                
                # Schedule market open subscriptions
                self.scheduler.add_job(
                    func=self.activate_market_subscriptions,
                    trigger='cron',
                    day_of_week=session.day_of_week,
                    hour=session.market_open.hour,
                    minute=session.market_open.minute,
                    id=f'market_open_{session.day_of_week}'
                )
                
                # Schedule market close cleanup
                self.scheduler.add_job(
                    func=self.cleanup_market_subscriptions,
                    trigger='cron',
                    day_of_week=session.day_of_week,
                    hour=session.market_close.hour,
                    minute=session.market_close.minute + 5,  # Cleanup 5 mins after close
                    id=f'market_close_{session.day_of_week}'
                )
        
        self.scheduler.start()
    
    def is_market_hours(self):
        """Check if current time is within trading hours"""
        now = datetime.now(pytz.timezone(self.template.timezone))
        current_day = now.weekday()
        current_time = now.time()
        
        # Check holidays first
        if self.is_holiday(now.date()):
            return False
        
        # Check regular sessions
        session = self.get_session_for_day(current_day)
        if not session or not session.is_trading_day:
            return False
            
        # Check if within any active session
        if (session.pre_market_start <= current_time <= session.pre_market_end or
            session.market_open <= current_time <= session.market_close or
            session.post_market_start <= current_time <= session.post_market_end):
            return True
            
        return False
    
    def get_next_market_open(self):
        """Calculate time until next market opening"""
        now = datetime.now(pytz.timezone(self.template.timezone))
        
        for days_ahead in range(1, 8):  # Check next 7 days
            check_date = now + timedelta(days=days_ahead)
            if not self.is_holiday(check_date.date()):
                session = self.get_session_for_day(check_date.weekday())
                if session and session.is_trading_day:
                    next_open = datetime.combine(
                        check_date.date(),
                        session.market_open,
                        tzinfo=pytz.timezone(self.template.timezone)
                    )
                    return next_open
        
        return None
```

### 6. Automatic Startup & Lifecycle Management with Multi-Account Failover

#### Account Failover Hierarchy
```python
class AccountFailoverManager:
    """
    Manages automatic account failover for uninterrupted option chain monitoring
    
    Failover Hierarchy:
    1. Primary Account (User's selected primary trading account)
    2. Secondary Accounts (Other active accounts from same broker)
    3. Cross-Broker Failover (Accounts from different brokers)
    """
    
    def __init__(self):
        self.failover_priority = []
        self.current_active_account = None
        self.failover_enabled = True
        self.max_failover_attempts = 3
        
    def build_failover_hierarchy(self, user_id):
        """
        Build intelligent failover hierarchy based on:
        1. Account health scores
        2. Broker reliability
        3. API rate limits
        4. Historical performance
        """
        accounts = TradingAccount.query.filter_by(
            user_id=user_id,
            is_active=True
        ).all()
        
        # Sort accounts by priority
        primary = next((acc for acc in accounts if acc.is_primary), None)
        same_broker = [acc for acc in accounts 
                      if acc.broker == primary.broker and not acc.is_primary]
        diff_broker = [acc for acc in accounts 
                      if acc.broker != primary.broker]
        
        # Build hierarchy
        self.failover_priority = [primary] + same_broker + diff_broker
        
        logger.info(f"Failover hierarchy built: {len(self.failover_priority)} accounts")
        return self.failover_priority
```

#### Enhanced Option Chain Lifecycle with Failover
```python
class OptionChainLifecycle:
    def __init__(self):
        self.nifty_chain = None
        self.banknifty_chain = None
        self.monitoring_active = False
        self.failover_manager = AccountFailoverManager()
        self.websocket_manager = ProfessionalWebSocketManager()
        
    def on_primary_account_connected(self, account):
        """
        Automatically triggered when primary account connects
        Sets up failover hierarchy and starts monitoring
        """
        if not self.monitoring_active:
            # Build failover hierarchy
            backup_accounts = self.failover_manager.build_failover_hierarchy(
                account.user_id
            )
            
            # Initialize WebSocket with failover support
            self.websocket_manager.create_connection_pool(
                primary_account=account,
                backup_accounts=backup_accounts[1:]  # Exclude primary
            )
            
            # Get current expiry dates
            nifty_expiry = self.get_current_expiry('NIFTY')
            banknifty_expiry = self.get_current_expiry('BANKNIFTY')
            
            # Start background monitoring with failover capability
            self.nifty_chain = OptionChainManager(
                underlying='NIFTY',
                expiry=nifty_expiry,
                websocket_manager=self.websocket_manager
            )
            self.banknifty_chain = OptionChainManager(
                underlying='BANKNIFTY',
                expiry=banknifty_expiry,
                websocket_manager=self.websocket_manager
            )
            
            # Start monitoring
            self.nifty_chain.start_monitoring()
            self.banknifty_chain.start_monitoring()
            
            self.monitoring_active = True
            logger.info(f"Option chains started with {len(backup_accounts)} failover accounts")
    
    def handle_account_failure(self, failed_account, error_type):
        """
        Intelligent failure handling with automatic switchover
        
        Error Types & Actions:
        - 'auth_failed': Invalid API key → Switch to next account
        - 'rate_limit': API limit exceeded → Temporary switch
        - 'connection_lost': Network issue → Retry then switch
        - 'account_suspended': Broker suspension → Immediate switch
        """
        logger.warning(f"Account failure: {failed_account.name} - {error_type}")
        
        if error_type in ['auth_failed', 'account_suspended']:
            # Immediate switchover required
            next_account = self.failover_manager.get_next_account()
            if next_account:
                self.switch_to_account(next_account)
                self.notify_user_of_switchover(failed_account, next_account)
            else:
                self.enter_degraded_mode()
                
        elif error_type == 'rate_limit':
            # Temporary switch with auto-recovery
            self.temporary_account_switch(duration_minutes=15)
            
        elif error_type == 'connection_lost':
            # Retry with exponential backoff
            self.retry_with_backoff(failed_account)
    
    def on_primary_account_disconnected(self):
        """
        Graceful shutdown or switch to backup
        """
        if self.monitoring_active:
            if self.failover_manager.has_backup_accounts():
                # Switch to first backup account
                backup = self.failover_manager.get_next_account()
                self.switch_to_account(backup)
                logger.info(f"Primary disconnected, switched to: {backup.name}")
            else:
                # No backups available, stop monitoring
                self.stop_all_monitoring()
```

#### Integration with Account Management
- Hook into `TradingAccount` model's `set_primary()` method
- Start option chains immediately upon successful primary account connection
- Maintain monitoring even when user navigates away from option chain page
- Stop monitoring only when primary account is disconnected or changed

### 7. Multi-Level Failover Architecture

#### Failover Levels Explained

##### Level 1: WebSocket Connection Failover (Same Account)
- **Primary WebSocket** → **Backup WebSocket** (same account)
- Handles temporary network issues
- Instant switchover (<1 second)
- No data loss

##### Level 2: Account Failover (Different Accounts)
- **Primary Account** → **Secondary Account** (same/different broker)
- Handles account-level failures (API key issues, account suspension)
- Switchover time: 5-10 seconds
- All subscriptions transferred

##### Level 3: Cross-Broker Failover
- **Broker A Accounts** → **Broker B Accounts**
- Handles broker-wide outages
- Maintains service continuity
- Different API endpoints used

#### Failover Decision Matrix
| Failure Type | Level 1 | Level 2 | Level 3 | Action |
|-------------|---------|---------|---------|---------|
| WebSocket disconnect | ✓ | | | Reconnect same account |
| Auth failure | | ✓ | | Switch to next account |
| Rate limit exceeded | | ✓ | | Temporary account switch |
| Account suspended | | ✓ | | Permanent account switch |
| Broker API down | | | ✓ | Switch to different broker |
| Network timeout | ✓ | | | Retry with backoff |

### 8. Professional WebSocket Management

#### Enterprise-Grade Connection Management with Account Failover
```python
class ProfessionalWebSocketManager:
    def __init__(self):
        self.connection_pool = {}
        self.max_connections = 10
        self.heartbeat_interval = 30  # seconds
        self.reconnect_attempts = 5
        self.backoff_strategy = ExponentialBackoff(base=2, max_delay=60)
        self.metrics = ConnectionMetrics()
        self.account_failover_enabled = True
        
    def create_connection_pool(self, primary_account, backup_accounts=None):
        """
        Create managed connection pool with multi-account failover capability
        
        Failover Strategy:
        1. Primary Account Connection - Main WebSocket connection
        2. Secondary Account Failover - If primary fails, switch to next available account
        3. Dual Connection per Account - Each account has primary and backup WebSocket connections
        """
        pool = {
            'current_account': primary_account,
            'backup_accounts': backup_accounts or [],
            'connections': {},
            'status': 'initializing',
            'failover_history': [],
            'metrics': {
                'account_switches': 0,
                'total_failures': 0,
                'current_health': 100
            }
        }
        
        # Initialize primary account connections
        pool['connections']['primary'] = {
            'account': primary_account,
            'ws_primary': self.create_websocket_client(primary_account, 'primary'),
            'ws_backup': self.create_websocket_client(primary_account, 'backup'),
            'status': 'active',
            'failure_count': 0
        }
        
        # Pre-initialize backup account connections (idle state)
        for idx, backup_account in enumerate(backup_accounts[:3]):  # Limit to 3 backup accounts
            pool['connections'][f'backup_{idx}'] = {
                'account': backup_account,
                'ws_primary': None,  # Created on-demand during failover
                'ws_backup': None,
                'status': 'standby',
                'failure_count': 0
            }
        
        return pool
    
    def handle_account_failure(self, failed_account):
        """
        Handle complete account failure by switching to next available account
        
        Failover Process:
        1. Detect primary account failure (API key invalid, account suspended, etc.)
        2. Select next healthy backup account
        3. Activate backup account connections
        4. Transfer all subscriptions to new account
        5. Update primary account reference
        """
        logger.error(f"Account failure detected: {failed_account.name}")
        
        # Find next available account
        next_account = self.get_next_healthy_account()
        
        if not next_account:
            logger.critical("No backup accounts available for failover!")
            self.notify_admin_critical_failure()
            return False
        
        # Perform account switchover
        logger.info(f"Switching to backup account: {next_account.name}")
        
        # Save current subscriptions
        current_subscriptions = self.get_all_subscriptions()
        
        # Close failed account connections
        self.close_account_connections(failed_account)
        
        # Activate new account
        self.activate_account(next_account)
        
        # Restore subscriptions on new account
        self.restore_subscriptions(next_account, current_subscriptions)
        
        # Update primary account reference
        self.pool['current_account'] = next_account
        self.pool['failover_history'].append({
            'timestamp': datetime.now(),
            'from_account': failed_account.name,
            'to_account': next_account.name,
            'reason': 'account_failure'
        })
        
        # Log failover event
        activity = ActivityLog(
            user_id=current_user.id,
            action='websocket_account_failover',
            details=f'Switched from {failed_account.name} to {next_account.name}'
        )
        db.session.add(activity)
        db.session.commit()
        
        return True
    
    def intelligent_subscription_management(self):
        """Optimize subscriptions based on current needs"""
        active_strategies = self.get_active_strategies()
        required_symbols = set()
        
        # Collect all required symbols
        for strategy in active_strategies:
            required_symbols.update(strategy.get_required_symbols())
        
        # Add option chain symbols
        if self.option_chain_active:
            required_symbols.update(self.get_option_chain_symbols())
        
        # Batch subscriptions for efficiency
        batch_size = 50  # OpenAlgo typical batch limit
        symbol_batches = [list(required_symbols)[i:i+batch_size] 
                         for i in range(0, len(required_symbols), batch_size)]
        
        for batch in symbol_batches:
            self.subscribe_batch(batch)
```

#### Resource Optimization & Rate Limiting
```python
class ResourceOptimizer:
    def __init__(self):
        self.subscription_limit = 200  # Max concurrent subscriptions
        self.message_rate_limit = 1000  # Messages per second
        self.memory_threshold = 500  # MB
        self.cpu_threshold = 70  # Percent
        
    def optimize_subscriptions(self, current_subscriptions):
        """Dynamically adjust subscriptions based on resource usage"""
        current_usage = self.get_resource_usage()
        
        if current_usage['memory'] > self.memory_threshold:
            # Reduce subscription depth
            return self.reduce_subscription_depth(current_subscriptions)
        
        if current_usage['cpu'] > self.cpu_threshold:
            # Throttle update frequency
            return self.throttle_updates(current_subscriptions)
        
        return current_subscriptions
```

### 8. Real-time Update Mechanism with Depth Processing

#### Enhanced WebSocket Event Handling
```python
class WebSocketDataProcessor:
    def __init__(self):
        self.quote_handler = QuoteHandler()
        self.depth_handler = DepthHandler()
        
    def on_data_received(self, data):
        """
        Process incoming WebSocket data based on subscription mode
        """
        mode = data.get('mode')
        symbol = data.get('symbol')
        
        if mode == 'quote':
            self.handle_quote_update(data)
        elif mode == 'depth':
            self.handle_depth_update(data)
    
    def handle_quote_update(self, data):
        """Process quote mode data (underlying indices)"""
        symbol = data['symbol']
        
        if symbol in ['NIFTY', 'BANKNIFTY']:
            # Update underlying data
            self.quote_handler.update_underlying({
                'symbol': symbol,
                'ltp': data['ltp'],
                'bid': data.get('bid'),
                'ask': data.get('ask'),
                'volume': data.get('volume')
            })
            
            # Check if ATM needs recalculation
            if self.check_atm_change(symbol, data['ltp']):
                self.recalculate_option_chain(symbol)
    
    def handle_depth_update(self, data):
        """Process depth mode data (option strikes)"""
        symbol = data['symbol']
        
        # Extract market depth information
        depth_info = {
            'symbol': symbol,
            'ltp': data.get('ltp', 0),
            'bids': data.get('bids', []),  # Array of bid levels
            'asks': data.get('asks', []),  # Array of ask levels
            'volume': data.get('volume', 0),
            'oi': data.get('oi', 0),
            'timestamp': data.get('timestamp')
        }
        
        # Process top-level bid/ask
        if depth_info['bids'] and depth_info['asks']:
            top_bid = depth_info['bids'][0]
            top_ask = depth_info['asks'][0]
            
            processed_depth = {
                'symbol': symbol,
                'ltp': depth_info['ltp'],
                'bid': top_bid['price'],
                'bid_qty': top_bid['quantity'],
                'ask': top_ask['price'],
                'ask_qty': top_ask['quantity'],
                'spread': top_ask['price'] - top_bid['price'],
                'spread_percent': ((top_ask['price'] - top_bid['price']) / depth_info['ltp']) * 100,
                'volume': depth_info['volume'],
                'oi': depth_info['oi']
            }
            
            # Update option chain with depth data
            self.depth_handler.update_option_depth(processed_depth)
            
            # Trigger order management checks
            self.check_order_conditions(processed_depth)
            
            # Monitor stop-loss conditions
            self.monitor_stop_loss(processed_depth)
```

#### Background Monitoring Services
- **Stop-Loss Monitor**: Continuously checks option premiums against defined SL levels
- **Target Monitor**: Tracks premium movements for target achievement
- **Alert Service**: Triggers notifications for significant premium changes
- **Data Persistence**: Maintains last 100 ticks for each symbol in memory

#### Data Broadcasting
- Use Server-Sent Events (SSE) or WebSocket to push updates to frontend
- Implement rate limiting to prevent excessive updates
- Zero-config cache mechanism for efficient data retrieval

### 9. Trading Hours Template UI

#### Admin Interface for Trading Hours Management
The Trading Hours Template page provides administrators with a comprehensive interface to configure market timings, holidays, and WebSocket scheduling behavior.

##### Key Features
1. **Visual Schedule Editor**: Drag-and-drop interface for session timings
2. **Holiday Calendar**: Integrated calendar with NSE/BSE holiday imports
3. **Live WebSocket Monitoring**: Real-time connection status and metrics
4. **Template Presets**: Pre-configured templates for different markets
5. **Bulk Operations**: Import/export templates in JSON format

##### Template Management Routes
```python
# app/admin/trading_hours_routes.py
@admin_bp.route('/trading-hours')
@login_required
@admin_required
def trading_hours_dashboard():
    """Main trading hours configuration dashboard"""
    templates = TradingHoursTemplate.query.all()
    active_template = TradingHoursTemplate.query.filter_by(is_active=True).first()
    ws_status = websocket_manager.get_status()
    
    return render_template('admin/trading_hours.html',
                         templates=templates,
                         active_template=active_template,
                         ws_status=ws_status)

@admin_bp.route('/api/trading-hours/apply', methods=['POST'])
@login_required
@admin_required
def apply_trading_template():
    """Apply trading hours template immediately"""
    template_id = request.json.get('template_id')
    template = TradingHoursTemplate.query.get_or_404(template_id)
    
    # Deactivate current template
    TradingHoursTemplate.query.update({'is_active': False})
    
    # Activate new template
    template.is_active = True
    db.session.commit()
    
    # Restart WebSocket scheduler with new template
    websocket_scheduler.apply_template(template)
    
    # Log the change
    activity = ActivityLog(
        user_id=current_user.id,
        action='trading_hours_changed',
        details=f'Applied template: {template.name}'
    )
    db.session.add(activity)
    db.session.commit()
    
    return jsonify({
        'status': 'success',
        'message': f'Template "{template.name}" applied successfully',
        'next_session': websocket_scheduler.get_next_session_info()
    })
```

### 10. Frontend Integration

#### Enhanced Option Chain Display with Market Depth
```html
<div class="option-chain-container">
    <table class="option-chain-table">
        <thead>
            <tr>
                <!-- CE Side -->
                <th>OI</th>
                <th>Volume</th>
                <th>Bid Qty</th>
                <th>Bid</th>
                <th>LTP</th>
                <th>Ask</th>
                <th>Ask Qty</th>
                <th>Spread</th>
                <!-- Strike Info -->
                <th class="strike-col">Strike</th>
                <th class="tag-col">Tag</th>
                <!-- PE Side -->
                <th>Spread</th>
                <th>Ask Qty</th>
                <th>Ask</th>
                <th>LTP</th>
                <th>Bid</th>
                <th>Bid Qty</th>
                <th>Volume</th>
                <th>OI</th>
            </tr>
        </thead>
        <tbody id="option-chain-data">
            <!-- Example row with depth data -->
            <tr class="atm-row">
                <!-- CE Data -->
                <td class="oi">1234567</td>
                <td class="volume">234567</td>
                <td class="bid-qty">750</td>
                <td class="bid-price">125.50</td>
                <td class="ltp ce-ltp">125.75</td>
                <td class="ask-price">126.00</td>
                <td class="ask-qty">600</td>
                <td class="spread">0.50</td>
                <!-- Strike -->
                <td class="strike">24500</td>
                <td class="tag atm-tag">ATM</td>
                <!-- PE Data -->
                <td class="spread">0.50</td>
                <td class="ask-qty">900</td>
                <td class="ask-price">125.50</td>
                <td class="ltp pe-ltp">125.25</td>
                <td class="bid-price">125.00</td>
                <td class="bid-qty">825</td>
                <td class="volume">198765</td>
                <td class="oi">987654</td>
            </tr>
        </tbody>
    </table>
</div>

<style>
/* Depth-based color coding */
.tight-spread { background-color: #4ade80; }  /* Green for tight spreads */
.normal-spread { background-color: #fbbf24; } /* Yellow for normal spreads */
.wide-spread { background-color: #f87171; }   /* Red for wide spreads */

.high-liquidity { font-weight: bold; color: #16a34a; }
.low-liquidity { color: #dc2626; opacity: 0.8; }

/* ATM row highlight */
.atm-row { 
    background-color: #dbeafe; 
    font-weight: bold; 
}

/* ITM/OTM gradients */
.itm-row { background: linear-gradient(to right, #fef3c7, #ffffff); }
.otm-row { background: linear-gradient(to right, #ffffff, #e0e7ff); }
</style>
```

#### Real-time Updates
- JavaScript WebSocket client for live updates
- Color coding for ITM/ATM/OTM visualization
- Highlighting for significant premium changes

### 11. Strategy Integration & Order Management Framework

#### Order Management with Market Depth
```python
class OptionOrderManager:
    """
    Intelligent order management using real-time bid/ask data
    """
    
    def __init__(self, option_chain_manager):
        self.option_chain = option_chain_manager
        self.slippage_tolerance = 0.5  # Points
        
    def place_smart_order(self, symbol, action, quantity, order_type='LIMIT'):
        """
        Place orders using real-time bid/ask for optimal execution
        """
        if order_type == 'LIMIT':
            # Get execution price based on market depth
            if action == 'BUY':
                # Place at ask or slightly above for quick execution
                ask_price = self.option_chain.get_option_ask(symbol)
                limit_price = ask_price + 0.05  # Small buffer
            else:  # SELL
                # Place at bid or slightly below
                bid_price = self.option_chain.get_option_bid(symbol)
                limit_price = bid_price - 0.05
            
            # Check spread before placing order
            spread = self.option_chain.get_option_spread(symbol)
            if spread > self.slippage_tolerance:
                # Wide spread - use more conservative pricing
                limit_price = self.calculate_mid_price(symbol)
            
            return self.execute_order(symbol, action, quantity, limit_price)
    
    def calculate_slippage(self, symbol, action, quantity):
        """
        Pre-calculate expected slippage based on depth
        """
        depth_data = self.option_chain.get_option_depth(symbol)
        
        if action == 'BUY':
            # Check ask liquidity
            available_qty = depth_data['ask_qty']
            if quantity > available_qty:
                # Not enough liquidity at top ask
                return self.calculate_impact_cost(symbol, quantity)
        else:
            # Check bid liquidity
            available_qty = depth_data['bid_qty']
            if quantity > available_qty:
                return self.calculate_impact_cost(symbol, quantity)
        
        return depth_data['spread'] * quantity  # Simple spread cost
```

#### Premium-based Stop Loss with Bid/Ask Consideration
```python
def calculate_premium_stops(option_chain, strategy_config):
    """
    Calculate stop-loss levels using bid/ask for accurate execution
    """
    stops = {}
    
    for tag in ['ATM', 'ITM1', 'ITM2', 'OTM1', 'OTM2']:
        option_data = option_chain.get_option_by_tag(tag)
        
        # For stop-loss, use bid price (worst case for exit)
        ce_bid = option_data['ce_data']['bid']
        pe_bid = option_data['pe_data']['bid']
        
        stops[tag] = {
            'ce_stop': ce_bid * (1 - strategy_config['stop_percent']/100),
            'pe_stop': pe_bid * (1 - strategy_config['stop_percent']/100),
            'ce_exit_price': ce_bid,  # Expected exit price
            'pe_exit_price': pe_bid,
            'spread_impact': option_data['ce_data']['spread']
        }
    
    return stops
```

#### Smart Entry/Exit with Depth Analysis
```python
class SmartExecutor:
    """
    Optimize entry/exit using market depth intelligence
    """
    
    def analyze_entry_opportunity(self, symbol):
        """
        Determine best entry based on bid/ask spread
        """
        depth = self.option_chain.get_option_depth(symbol)
        
        analysis = {
            'spread': depth['spread'],
            'spread_percent': (depth['spread'] / depth['ltp']) * 100,
            'liquidity_score': self.calculate_liquidity_score(depth),
            'recommendation': 'WAIT'  # Default
        }
        
        # Tight spread with good liquidity - good entry
        if analysis['spread_percent'] < 0.5 and analysis['liquidity_score'] > 80:
            analysis['recommendation'] = 'ENTER_NOW'
            analysis['suggested_price'] = depth['ask']
        # Wide spread - wait or use mid price
        elif analysis['spread_percent'] > 2:
            analysis['recommendation'] = 'WAIT_FOR_TIGHTER_SPREAD'
            analysis['suggested_price'] = (depth['bid'] + depth['ask']) / 2
        
        return analysis
    
    def monitor_exit_conditions(self, position):
        """
        Monitor bid price for exit conditions
        """
        symbol = position['symbol']
        depth = self.option_chain.get_option_depth(symbol)
        
        # For exits, monitor bid price (what we can sell at)
        current_exit_price = depth['bid']
        
        exit_signal = {
            'current_exit_price': current_exit_price,
            'spread': depth['spread'],
            'can_exit_now': depth['bid_qty'] >= position['quantity'],
            'slippage_if_exit': self.calculate_exit_slippage(position)
        }
        
        return exit_signal
```

#### Strike Selection Interface with Depth Metrics
- Real-time bid/ask display for each strike
- Spread percentage indicators
- Liquidity heat map
- Smart order recommendations based on depth

### 12. Performance Optimization

#### Caching Strategy (Zero-Config)
- **TTLCache** from `cachetools` library for in-memory caching with automatic expiration
- Thread-safe caching using `threading.Lock()` for concurrent access
- 30-second TTL for option chain data to balance freshness and performance
- LRU (Least Recently Used) eviction policy when cache size limit reached
- No external dependencies or configuration required

```python
from cachetools import TTLCache
import threading

class OptionChainCache:
    def __init__(self, maxsize=100, ttl=30):
        self.cache = TTLCache(maxsize=maxsize, ttl=ttl)
        self.lock = threading.Lock()
    
    def get(self, key):
        with self.lock:
            return self.cache.get(key)
    
    def set(self, key, value):
        with self.lock:
            self.cache[key] = value
```

#### Alternative Zero-Config Cache Options
1. **Simple Dict with Timestamp** - Manual TTL management
2. **Flask-Caching with SimpleCache** - Flask's built-in memory backend
3. **functools.lru_cache** - For method-level caching

#### Rate Limiting
- WebSocket connection pooling
- Smart subscription management (subscribe only to visible strikes)
- Throttled updates to prevent frontend overload

## Use Cases for Background Monitoring

### Real-time Strategy Management
1. **Stop-Loss Monitoring**
   - Continuously track option premiums against predefined SL levels
   - Automatic alert generation when SL is approaching
   - Execute square-off orders when SL is hit

2. **Target Tracking**
   - Monitor profit targets for open positions
   - Calculate real-time P&L based on premium movements
   - Trigger notifications for target achievement

3. **Premium-based Adjustments**
   - Dynamic position sizing based on premium changes
   - Automatic hedge adjustments for risk management
   - Real-time Greeks calculation for portfolio optimization

4. **Multi-Strategy Coordination**
   - Support multiple strategies running simultaneously
   - Cross-strategy risk management
   - Portfolio-level margin and exposure monitoring

### Data Availability Benefits
- **Instant Access**: Option chain data readily available without API calls
- **Historical Tracking**: Maintain tick history for backtesting
- **Performance Analytics**: Real-time strategy performance metrics
- **Risk Dashboard**: Continuous risk monitoring across all positions

## Implementation Phases

### Phase 1: Core Infrastructure
1. Option chain data models and calculations
2. OpenAlgo WebSocket integration
3. Basic Flask route and JSON API
4. Strike tagging system

### Phase 2: Real-time Interface
1. WebSocket manager implementation
2. Frontend option chain table
3. Live premium updates
4. Connection health monitoring

### Phase 3: Strategy Integration (Future)
1. Premium-based stop-loss calculations
2. Strategy builder interface
3. Strike selection tools
4. Advanced analytics and alerts

## Security & Error Handling

### Error Scenarios
- OpenAlgo API connectivity issues
- WebSocket connection failures
- Invalid symbol or expiry data
- Market hours validation

### Security Considerations
- Rate limiting on option chain endpoints
- Input validation for underlying and expiry parameters
- Secure WebSocket connections with proper authentication
- Data sanitization for frontend display

## Monitoring & Logging

### Key Metrics
- WebSocket connection uptime
- Data update latency
- API response times
- Error rates and types

### Logging Requirements
- Option chain initialization events
- WebSocket connection status
- Premium update frequencies
- Performance bottlenecks

## Professional-Grade Architecture Summary

This comprehensive option chain architecture delivers enterprise-level capabilities through:

### 🎯 **Core Professional Features**

1. **Automatic Background Monitoring**
   - Starts automatically when primary account connects
   - Continuous monitoring for NIFTY & BANKNIFTY
   - No user intervention required
   - Persistent data availability for strategies

2. **Intelligent Trading Hours Management**
   - Configurable trading hour templates
   - Automatic WebSocket subscription/unsubscription
   - Market holiday handling
   - Pre-market and post-market session support
   - Timezone-aware scheduling (IST default)

3. **Enterprise WebSocket Management**
   - Connection pooling with primary/backup failover
   - Exponential backoff retry strategy
   - Health monitoring with heartbeat checks
   - Resource-aware subscription optimization
   - Batch processing for efficient API usage

4. **Professional Resource Management**
   - Dynamic throttling based on system resources
   - Memory and CPU threshold monitoring
   - Smart subscription prioritization
   - Message rate limiting with market volatility awareness

5. **Zero-Config Caching**
   - TTLCache for automatic expiration
   - Thread-safe operations
   - No external dependencies
   - LRU eviction policy

### 📊 **Performance Metrics**

- **Subscription Capacity**: 200+ concurrent symbols
- **Message Processing**: 1000-2000 messages/second
- **Latency**: <50ms for premium updates
- **Uptime Target**: 99.9% during market hours
- **Memory Footprint**: <500MB for full option chains
- **Recovery Time**: <5 seconds for connection failures

### 🔧 **Operational Excellence**

1. **Automated Scheduling**
   - Cron-based session management
   - Automatic reconnection logic
   - Holiday calendar integration
   - Expiry rollover handling

2. **Monitoring & Observability**
   - Real-time WebSocket metrics
   - Connection health scoring
   - Performance dashboards
   - Comprehensive audit logging

3. **Failure Recovery**
   - Automatic failover to backup connections
   - State preservation during disconnections
   - Intelligent resubscription on recovery
   - Graceful degradation under load

### 🚀 **Production Readiness**

The architecture ensures production-grade reliability through:
- **High Availability**: Redundant connections and failover mechanisms
- **Scalability**: Resource-aware scaling and optimization
- **Maintainability**: Modular design with clear separation of concerns
- **Security**: Encrypted connections with authentication
- **Compliance**: Comprehensive audit trails and activity logging

This professional-grade implementation provides a robust foundation for real-time options trading, ensuring reliable data availability for stop-loss monitoring, target tracking, and advanced strategy execution.