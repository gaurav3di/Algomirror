"""
Background Service for Automatic Option Chain Monitoring
Automatically starts option chains when primary account connects
"""

import threading
import logging
from datetime import datetime, time
import time as time_module
from typing import Optional, Dict, Any
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

from app.models import TradingAccount, TradingHoursTemplate, TradingSession, MarketHoliday
from app.utils.option_chain import OptionChainManager
from app.utils.websocket_manager import ProfessionalWebSocketManager
from app.utils.openalgo_client import ExtendedOpenAlgoAPI

logger = logging.getLogger(__name__)


class OptionChainBackgroundService:
    """
    Background service that automatically starts option chain monitoring
    when a primary account connects
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
            
        self.scheduler = BackgroundScheduler(timezone=pytz.timezone('Asia/Kolkata'))
        self.active_managers = {}
        self.websocket_managers = {}
        self.is_running = False
        self.primary_account = None
        self.backup_accounts = []
        self._initialized = True
        
        logger.info("Option Chain Background Service initialized")
    
    def start_service(self):
        """Start the background service"""
        if not self.is_running:
            self.scheduler.start()
            self.is_running = True
            logger.info("Background service started")
            
            # Schedule market hours check
            self.schedule_market_hours()
    
    def stop_service(self):
        """Stop the background service"""
        if self.is_running:
            # Stop all option chain managers
            for underlying in list(self.active_managers.keys()):
                self.stop_option_chain(underlying)
            
            self.scheduler.shutdown(wait=False)
            self.is_running = False
            logger.info("Background service stopped")
    
    def on_primary_account_connected(self, account: TradingAccount):
        """
        Called when primary account successfully connects
        Automatically starts NIFTY, BANKNIFTY, and SENSEX option chains
        """
        try:
            logger.info(f"Primary account connected: {account.account_name}")
            self.primary_account = account
            
            # Get backup accounts for failover
            self.backup_accounts = TradingAccount.query.filter_by(
                user_id=account.user_id,
                is_active=True,
                is_primary=False
            ).order_by(TradingAccount.created_at).all()
            
            # Check if within trading hours
            if self.is_trading_hours():
                # Start option chains in background to avoid blocking Flask startup
                import threading
                def start_chains():
                    time_module.sleep(2)  # Give Flask time to start
                    # Start first 4 expiries for each index
                    self.start_option_chain('NIFTY')  # Will load 4 expiries
                    self.start_option_chain('BANKNIFTY')  # Will load 4 expiries
                    self.start_option_chain('SENSEX')  # Will load 4 expiries
                    logger.info("Option chains started automatically for NIFTY, BANKNIFTY, and SENSEX with 4 expiries each")
                
                thread = threading.Thread(target=start_chains)
                thread.daemon = True
                thread.start()
            else:
                logger.info("Outside trading hours, option chains will start at market open")
                
        except Exception as e:
            logger.error(f"Error starting option chains on account connection: {e}")
    
    def on_account_disconnected(self, account: TradingAccount):
        """
        Called when an account disconnects
        Triggers failover if it was the primary account
        """
        if account == self.primary_account:
            logger.warning(f"Primary account disconnected: {account.account_name}")
            self.attempt_failover()
    
    def attempt_failover(self):
        """Attempt to failover to backup account"""
        if not self.backup_accounts:
            logger.error("No backup accounts available for failover")
            self.stop_all_option_chains()
            return
        
        # Try next backup account
        next_account = self.backup_accounts.pop(0)
        logger.info(f"Attempting failover to: {next_account.account_name}")
        
        try:
            # Test connection
            client = ExtendedOpenAlgoAPI(
                api_key=next_account.get_api_key(),
                host=next_account.host_url
            )
            
            ping_response = client.ping()
            if ping_response.get('status') == 'success':
                # Update primary account
                self.primary_account = next_account
                
                # Restart all active option chains with new account
                active_underlyings = set()
                for key in self.active_managers.keys():
                    underlying = key.split('_')[0]
                    active_underlyings.add(underlying)
                
                for underlying in active_underlyings:
                    self.restart_option_chain(underlying)
                
                logger.info(f"Failover successful to: {next_account.account_name}")
            else:
                # Try next backup
                self.attempt_failover()
                
        except Exception as e:
            logger.error(f"Failover failed for {next_account.account_name}: {e}")
            self.attempt_failover()
    
    def start_option_chain(self, underlying: str, expiry: str = None):
        """Start option chain monitoring for specified underlying and expiry"""
        if not self.primary_account:
            logger.warning("No primary account available, attempting failover")
            # Try to failover to a backup account
            if self.backup_accounts:
                self.attempt_failover()
                # After failover attempt, check if we have a primary now
                if not self.primary_account:
                    logger.error("Failover failed - no accounts available")
                    return False
            else:
                logger.error("No primary or backup accounts available")
                return False
        
        try:
            # Create API client - try primary first, then backup
            client = ExtendedOpenAlgoAPI(
                api_key=self.primary_account.get_api_key(),
                host=self.primary_account.host_url
            )
            
            # Get expiry dates if not provided
            if not expiry:
                expiry_response = client.expiry(
                    symbol=underlying,
                    exchange='BFO' if underlying == 'SENSEX' else 'NFO',
                    instrumenttype='options'
                )
                
                # If primary fails, try backup accounts
                if expiry_response.get('status') != 'success':
                    logger.warning(f"Primary account failed to get expiry for {underlying}, trying backup accounts")
                    
                    for backup in self.backup_accounts:
                        logger.info(f"Trying backup account: {backup.account_name}")
                        backup_client = ExtendedOpenAlgoAPI(
                            api_key=backup.get_api_key(),
                            host=backup.host_url
                        )
                        
                        expiry_response = backup_client.expiry(
                            symbol=underlying,
                            exchange='BFO' if underlying == 'SENSEX' else 'NFO',
                            instrumenttype='options'
                        )
                        
                        if expiry_response.get('status') == 'success':
                            logger.info(f"Successfully got expiry from backup account: {backup.account_name}")
                            client = backup_client  # Use backup client for further operations
                            break
                    else:
                        logger.error(f"All accounts failed to get expiry for {underlying}")
                        return False
                
                expiries = expiry_response.get('data', [])
                if not expiries:
                    logger.error(f"No expiries available for {underlying}")
                    return False
                
                # Get first 4 expiries for streaming
                expiries_to_use = expiries[:4] if len(expiries) >= 4 else expiries
            else:
                expiries_to_use = [expiry]
            
            all_managers_started = True
            
            # Start manager for each expiry
            for exp in expiries_to_use:
                manager_key = f"{underlying}_{exp}"
                
                if manager_key in self.active_managers:
                    logger.info(f"Option chain already running for {manager_key}")
                    continue
            
                # Create or get WebSocket manager for this underlying
                ws_manager_key = underlying
                if ws_manager_key not in self.websocket_managers:
                    ws_manager = ProfessionalWebSocketManager()
                    ws_manager.create_connection_pool(
                        primary_account=self.primary_account,
                        backup_accounts=self.backup_accounts
                    )
                    
                    # Connect WebSocket with failover support
                    if hasattr(self.primary_account, 'websocket_url'):
                        connected = ws_manager.connect(
                            ws_url=self.primary_account.websocket_url,
                            api_key=self.primary_account.get_api_key()
                        )
                        
                        # If primary failed, ws_manager would have tried failover
                        # Check if we're now connected to a different account
                        current_account = ws_manager.connection_pool.get('current_account')
                        if current_account and current_account != self.primary_account:
                            logger.info(f"WebSocket failover occurred: now using {current_account.account_name}")
                        
                        # Wait for authentication to complete (max 5 seconds)
                        auth_wait_time = 0
                        while not ws_manager.authenticated and auth_wait_time < 5:
                            time_module.sleep(0.5)
                            auth_wait_time += 0.5
                            
                        if not ws_manager.authenticated:
                            logger.error(f"WebSocket authentication failed for {underlying} after failover attempts")
                            all_managers_started = False
                            continue
                        
                        logger.info(f"WebSocket authenticated for {underlying}")
                    
                    self.websocket_managers[ws_manager_key] = ws_manager
                else:
                    ws_manager = self.websocket_managers[ws_manager_key]
                
                # Create option chain manager for this expiry
                option_manager = OptionChainManager(
                    underlying=underlying,
                    expiry=exp,
                    websocket_manager=ws_manager
                )
                
                # Initialize with API client
                option_manager.initialize(client)
                
                # Start monitoring
                option_manager.start_monitoring()
                
                # Store managers with unique key
                self.active_managers[manager_key] = option_manager
                
                logger.info(f"Option chain started for {manager_key}")
            
            return all_managers_started
            
        except Exception as e:
            logger.error(f"Error starting option chain for {underlying}: {e}")
            return False
    
    def stop_option_chain(self, underlying: str, expiry: str = None):
        """Stop option chain monitoring for specified underlying and optionally expiry"""
        try:
            if expiry:
                # Stop specific expiry
                manager_key = f"{underlying}_{expiry}"
                if manager_key in self.active_managers:
                    manager = self.active_managers[manager_key]
                    manager.stop_monitoring()
                    del self.active_managers[manager_key]
                    logger.info(f"Option chain stopped for {manager_key}")
            else:
                # Stop all expiries for this underlying
                keys_to_remove = [k for k in self.active_managers.keys() if k.startswith(f"{underlying}_")]
                for key in keys_to_remove:
                    manager = self.active_managers[key]
                    manager.stop_monitoring()
                    del self.active_managers[key]
                    logger.info(f"Option chain stopped for {key}")
                
                # Disconnect WebSocket for this underlying
                if underlying in self.websocket_managers:
                    ws_manager = self.websocket_managers[underlying]
                    ws_manager.disconnect()
                    del self.websocket_managers[underlying]
                
        except Exception as e:
            logger.error(f"Error stopping option chain for {underlying}: {e}")
    
    def restart_option_chain(self, underlying: str, expiry: str = None):
        """Restart option chain with current primary account"""
        logger.info(f"Restarting option chain for {underlying} {expiry or 'all expiries'}")
        self.stop_option_chain(underlying, expiry)
        self.start_option_chain(underlying, expiry)
    
    def stop_all_option_chains(self):
        """Stop all active option chains"""
        for underlying in list(self.active_managers.keys()):
            self.stop_option_chain(underlying)
    
    def schedule_market_hours(self):
        """Schedule option chains based on trading hours"""
        try:
            # Default NSE trading hours if no template exists
            # Monday-Friday: 9:15 AM to 3:30 PM IST
            for day in range(5):  # 0=Monday to 4=Friday
                # Schedule market open
                self.scheduler.add_job(
                    func=self.on_market_open,
                    trigger=CronTrigger(
                        day_of_week=day,
                        hour=9,
                        minute=15,
                        timezone=pytz.timezone('Asia/Kolkata')
                    ),
                    id=f"market_open_{day}",
                    replace_existing=True
                )
                
                # Schedule market close
                self.scheduler.add_job(
                    func=self.on_market_close,
                    trigger=CronTrigger(
                        day_of_week=day,
                        hour=15,
                        minute=30,
                        timezone=pytz.timezone('Asia/Kolkata')
                    ),
                    id=f"market_close_{day}",
                    replace_existing=True
                )
            
            logger.info("Market hours scheduled with default NSE timings")
            
        except Exception as e:
            logger.error(f"Error scheduling market hours: {e}")
    
    def on_market_open(self):
        """Called when market opens"""
        logger.info("Market opened - starting option chains")
        
        if self.primary_account:
            if not self.is_holiday():
                self.start_option_chain('NIFTY')
                self.start_option_chain('BANKNIFTY')
                self.start_option_chain('SENSEX')
    
    def on_market_close(self):
        """Called when market closes"""
        logger.info("Market closed - stopping option chains")
        self.stop_all_option_chains()
    
    def is_trading_hours(self) -> bool:
        """Check if current time is within trading hours"""
        try:
            now = datetime.now(pytz.timezone('Asia/Kolkata'))
            current_day = now.weekday()
            current_time = now.time()
            current_date = now.date()
            
            # Check for special trading sessions first
            if self.has_special_session(current_date, current_time):
                return True
            
            # Check if holiday (skip if special session already checked)
            if self.is_holiday():
                return False
            
            # Default NSE hours (9:15 AM to 3:30 PM, Monday-Friday)
            if current_day >= 5:  # Saturday or Sunday
                return False
            return time(9, 15) <= current_time <= time(15, 30)
            
        except Exception as e:
            logger.error(f"Error checking trading hours: {e}")
            return False
    
    def has_special_session(self, check_date, check_time) -> bool:
        """Check if there's a special trading session at the given date and time"""
        try:
            # Import here to avoid circular imports
            from app.models import SpecialTradingSession
            from app import db
            
            # This would normally require app context
            # For now, return False as we can't query without context
            # In production, you'd cache special sessions or use a different approach
            return False
            
        except Exception as e:
            logger.debug(f"Could not check special sessions: {e}")
            return False
    
    def is_holiday(self) -> bool:
        """Check if today is a market holiday"""
        try:
            # For now, return False (no holiday checking without database context)
            # This can be enhanced later with proper app context handling
            return False
            
        except Exception as e:
            logger.error(f"Error checking holiday: {e}")
            return False
    
    def get_status(self) -> Dict[str, Any]:
        """Get current service status"""
        return {
            'is_running': self.is_running,
            'primary_account': self.primary_account.account_name if self.primary_account else None,
            'backup_accounts': len(self.backup_accounts),
            'active_option_chains': list(self.active_managers.keys()),
            'is_trading_hours': self.is_trading_hours(),
            'is_holiday': self.is_holiday(),
            'websocket_status': {
                underlying: ws.get_status() 
                for underlying, ws in self.websocket_managers.items()
            }
        }


# Global service instance
option_chain_service = OptionChainBackgroundService()