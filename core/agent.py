"""
Trading agent for Bluefin Exchange that analyzes TradingView charts and executes trades.

This script provides automated trading functionality by analyzing TradingView charts 
for the SUI/USD pair, confirming signals with Perplexity AI, and executing trades
on the Bluefin Exchange.

Requirements:
- Python 3.8+
- Required Python libraries:
  pip install python-dotenv playwright asyncio backoff
  python -m playwright install

- Either of the Bluefin client libraries:
  # For SUI integration
  pip install git+https://github.com/fireflyprotocol/bluefin-client-python-sui.git
  
  # OR for general v2 integration
  pip install git+https://github.com/fireflyprotocol/bluefin-v2-client-python.git

Environment variables:
- Set in .env file:
  # For SUI client
  BLUEFIN_PRIVATE_KEY=your_private_key_here
  BLUEFIN_NETWORK=MAINNET  # or TESTNET
  
  # For v2 client
  BLUEFIN_API_KEY=your_api_key_here
  BLUEFIN_API_SECRET=your_api_secret_here
  BLUEFIN_API_URL=optional_custom_url_here

Usage:
- Run: python agent.py
- Check config.py for configurable trading parameters

Reference:
- Bluefin API Documentation: https://bluefin-exchange.readme.io/reference/introduction
"""

import os
import sys
import time
import json
import asyncio
import random
import logging
import traceback
from datetime import datetime, timedelta
from pathlib import Path
import backoff
from dotenv import load_dotenv
try:
    from playwright.async_api import async_playwright
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    logging.warning("Playwright not installed. Browser automation will not work.")
    PLAYWRIGHT_AVAILABLE = False
    async_playwright = None
    sync_playwright = None
from typing import Dict, List, Optional, Union, Any, TypeVar, Type, cast
import requests
import base64
import aiohttp
from anthropic import Client, RateLimitError, APITimeoutError
import re
import tempfile
import argparse
import uvicorn
from fastapi import FastAPI, Request
import glob

# Fix the import for mock_perplexity
try:
    from .mock_perplexity import MockPerplexityClient
except ImportError:
    # Fallback for direct script execution
    try:
        from mock_perplexity import MockPerplexityClient
    except ImportError:
        from core.mock_perplexity import MockPerplexityClient

# Configure logging first
def setup_logging():
    """Set up logging configuration."""
    log_format = json.dumps({
        "timestamp": "%(asctime)s",
        "level": "%(levelname)s",
        "module": "%(module)s",
        "message": "%(message)s"
    })
    
    logging.basicConfig(
        level=logging.INFO,
        format=log_format,
        handlers=[
            logging.FileHandler(f"logs/trading_log_{int(datetime.now().timestamp())}.log"),
            logging.StreamHandler()
        ]
    )

logger = logging.getLogger("bluefin_agent")

# Load environment variables from .env file
load_dotenv()

# Set mock trading from environment variable
MOCK_TRADING = os.getenv("MOCK_TRADING", "True").lower() == "true"
if not MOCK_TRADING:
    logger.info("Live trading mode enabled - will execute real trades on Bluefin")
else:
    logger.info("Mock trading mode enabled - no real trades will be executed")

# Try to import configuration, with fallbacks if not available
try:
    from config import TRADING_PARAMS, RISK_PARAMS, AI_PARAMS, CLAUDE_CONFIG
except ImportError:
    logger.warning("Could not import configuration from config.py, using defaults")
    
    # Default trading parameters
    TRADING_PARAMS = {
        "chart_symbol": "BTCUSDT",
        "timeframe": "1h",
        "candle_type": "Heikin Ashi",
        "indicators": ["MACD", "RSI", "Bollinger Bands"],
        "min_confidence": 0.7,
        "analysis_interval_seconds": 300,
        "max_position_size_usd": 1000,
        "leverage": 5,
        "trading_symbol": "BTC-PERP",
        "stop_loss_percentage": 0.02,
        "take_profit_multiplier": 2
    }
    
    # Default risk parameters
    RISK_PARAMS = {
        "max_risk_per_trade": 0.02,  # 2% of account balance
        "max_open_positions": 3,
        "max_daily_loss": 0.05,  # 5% of account balance
        "min_risk_reward_ratio": 2.0
    }
    
    # Default AI parameters
    AI_PARAMS = {
        "use_perplexity": True,
        "use_claude": True,
        "perplexity_confidence_threshold": 0.7,
        "claude_confidence_threshold": 0.7,
        "confidence_concordance_required": True
    }

# Define default enums for order types and sides
class ORDER_SIDE_ENUM:
    BUY = "BUY"
    SELL = "SELL"

class ORDER_TYPE_ENUM:
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    STOP_MARKET = "STOP_MARKET"

# Initialize with default enums
ORDER_SIDE = ORDER_SIDE_ENUM
ORDER_TYPE = ORDER_TYPE_ENUM

# Type definitions to help with linting
# Use Any instead of TypeVar for better compatibility with linter
BluefinClientT = TypeVar('BluefinClientT')

# Blueprint type for BluefinClient - this should be first
BluefinClientType: Any = None

# Create a class that can be used as a type hint for BluefinClient
class BaseBluefinClient:
    async def close_position(self, position_id): pass
    async def get_account_equity(self): pass
    async def create_order(self, **kwargs): pass

# Initialize global variables
claude_client = None

# Import Anthropic API for Claude
try:
    from anthropic import Client, RateLimitError, APITimeoutError
    CLAUDE_AVAILABLE = True
except ImportError:
    logger.warning("Anthropic Python SDK not installed. Claude AI will not be available.")
    CLAUDE_AVAILABLE = False

# Try to import SUI client first
try:
    # Import according to official documentation
    from bluefin_v2_client import BluefinClient as BluefinSUIClient, Networks
    BLUEFIN_CLIENT_SUI_AVAILABLE = True
    BluefinClient = BluefinSUIClient
    logger.info("Bluefin v2 client available")
except ImportError:
    logger.warning("Bluefin v2 client not available, will try SUI client")
    BLUEFIN_CLIENT_SUI_AVAILABLE = False
    
    # Try to import SUI client
    try:
        # Import according to official documentation
        from bluefin_client_sui import BluefinClient as BluefinSUIOldClient, Networks
        BLUEFIN_V2_CLIENT_AVAILABLE = True
        BluefinClient = BluefinSUIOldClient
        logger.info("Bluefin SUI client available")
    except ImportError:
        logger.warning("Bluefin SUI client not available")
        BLUEFIN_V2_CLIENT_AVAILABLE = False
        logger.warning("Running in simulation mode without actual trading capabilities")
        print("WARNING: No Bluefin client libraries found. Using mock implementation.")
        print("Please install one of the following:")
        print("   pip install git+https://github.com/fireflyprotocol/bluefin-v2-client-python.git")
        print("   pip install git+https://github.com/fireflyprotocol/bluefin-client-python-sui.git")

# Warn if no Bluefin client libraries are available
if not BLUEFIN_CLIENT_SUI_AVAILABLE and not BLUEFIN_V2_CLIENT_AVAILABLE:
    logger.warning("No Bluefin client available, running in simulation mode")
    logger.info("Using MockBluefinClient for simulation")

# Create Networks mock class for the mock BluefinClient
class MockNetworks:
    """Mock networks for the MockBluefinClient"""
    MAINNET = "mainnet"
    TESTNET = "testnet"
    SUI_STAGING = "sui_staging"
    SUI_PROD = "sui_prod"

# Set up Networks
Networks = MockNetworks()

# Update BluefinClient variable definition
BluefinClient = None  # Will be set to either the real client or MockBluefinClient

# Update the mock BluefinClient to handle all methods needed
class MockBluefinClient:
    """Mock implementation of the Bluefin client for testing and development"""
    
    def __init__(self, are_terms_accepted=True, network=None, private_key=None, **kwargs):
        self.network = network or MockNetworks.TESTNET
        self.private_key = private_key or "mock_private_key"
        self.are_terms_accepted = are_terms_accepted
        self.leverage_settings = {
            'BTC-PERP': 5,
            'ETH-PERP': 5,
            'SUI-PERP': 5,
            'SOL-PERP': 5,
            'BNB-PERP': 5
        }
        self.orders = []  # Store mock orders
        logger.info(f"Initialized MockBluefinClient on {self.network}")
        
    async def init(self, onboard_user=False):
        """Mock implementation of init"""
        logger.info(f"[MOCK] Initialized client with onboard_user={onboard_user}")
        return True
        
    def get_public_address(self):
        """Mock implementation of get_public_address"""
        return "0xMOCK_ADDRESS_123456789"
        
    async def get_account_details(self):
        """Mock implementation of get_account_details"""
        return {
            "address": "0xMOCK_ADDRESS_123456789",
            "balance": 10000.0,
            "margin_balance": 5000.0,
            "positions": [],
            "orders": [],
            "account_type": "mock"
        }
        
    async def get_margin_bank_balance(self):
        """Mock implementation of get_margin_bank_balance
        Based on https://bluefin-exchange.readme.io/reference/get-deposit-withdraw-usdc-from-marginbank
        """
        return 5000.0
        
    async def deposit_margin_to_bank(self, amount):
        """Mock implementation of deposit_margin_to_bank
        Based on https://bluefin-exchange.readme.io/reference/get-deposit-withdraw-usdc-from-marginbank
        """
        logger.info(f"[MOCK] Depositing {amount} USDC to margin bank")
        return amount
        
    async def withdraw_margin_from_bank(self, amount):
        """Mock implementation of withdraw_margin_from_bank
        Based on https://bluefin-exchange.readme.io/reference/get-deposit-withdraw-usdc-from-marginbank
        """
        logger.info(f"[MOCK] Withdrawing {amount} USDC from margin bank")
        return amount
        
    async def withdraw_all_margin_from_bank(self):
        """Mock implementation of withdraw_all_margin_from_bank
        Based on https://bluefin-exchange.readme.io/reference/get-deposit-withdraw-usdc-from-marginbank
        """
        logger.info("[MOCK] Withdrawing all USDC from margin bank")
        balance = await self.get_margin_bank_balance()
        return balance
        
    async def get_orderbook(self, symbol):
        """Mock implementation of get_orderbook"""
        # Create mock orderbook data with realistic structure
        mock_prices = {
            'BTC-PERP': {'bid': 50000, 'ask': 50100},
            'ETH-PERP': {'bid': 3000, 'ask': 3010},
            'SUI-PERP': {'bid': 1.5, 'ask': 1.51},
            'SOL-PERP': {'bid': 100, 'ask': 101},
            'BNB-PERP': {'bid': 400, 'ask': 402}
        }
        
        # Extract base symbol from the full symbol
        base_symbol = symbol.split('-')[0] if '-' in symbol else symbol.split('/')[0] if '/' in symbol else symbol
        
        # Find matching price or use default
        price_data = None
        for key, data in mock_prices.items():
            if base_symbol in key:
                price_data = data
                break
        
        if not price_data:
            price_data = {'bid': 100, 'ask': 101}  # Default fallback
            
        # Create mock orderbook with realistic structure
        bid_price = price_data['bid']
        ask_price = price_data['ask']
        
        return {
            'bids': [
                [str(bid_price), '1.0'],
                [str(bid_price * 0.99), '2.0'],
                [str(bid_price * 0.98), '3.0'],
                [str(bid_price * 0.97), '5.0'],
                [str(bid_price * 0.96), '10.0']
            ],
            'asks': [
                [str(ask_price), '1.0'],
                [str(ask_price * 1.01), '2.0'],
                [str(ask_price * 1.02), '3.0'],
                [str(ask_price * 1.03), '5.0'],
                [str(ask_price * 1.04), '10.0']
            ],
            'timestamp': int(time.time() * 1000)
        }
        
    async def close_position(self, position_id):
        """Mock implementation of close_position"""
        logger.info(f"[MOCK] Closing position {position_id}")
        return {"success": True, "position_id": position_id, "status": "closed"}
        
    async def get_account_equity(self):
        """Mock implementation of get_account_equity"""
        equity = float(os.getenv("MOCK_ACCOUNT_EQUITY", "10000.0"))
        logger.info(f"[MOCK] Getting account equity: {equity}")
        return equity
        
    def create_order_signature_request(self, symbol, side, size, price=None, order_type="MARKET", **kwargs):
        """Mock implementation of create_order_signature_request
        Based on https://bluefin-exchange.readme.io/reference/sign-post-orders
        """
        logger.info(f"[MOCK] Creating order signature request for {side} {size} {symbol}")
        return MockOrderSignatureRequest(
            symbol=symbol,
            side=side,
            size=size,
            price=price,
            order_type=order_type,
            leverage=kwargs.get("leverage", 5),
            **kwargs
        )
    
    def create_signed_order(self, signature_request):
        """Mock implementation of create_signed_order
        Based on https://bluefin-exchange.readme.io/reference/sign-post-orders
        """
        logger.info(f"[MOCK] Creating signed order from signature request")
        order_hash = signature_request.get_order_hash()
        signature = f"0xMOCK_SIGNATURE_{get_timestamp()}"
        
        return {
            "orderHash": order_hash,
            "signature": signature,
            "symbol": signature_request.symbol,
            "side": signature_request.side,
            "size": signature_request.size,
            "price": signature_request.price,
            "orderType": signature_request.order_type,
            "leverage": signature_request.leverage
        }
    
    async def post_signed_order(self, signed_order):
        """Mock implementation of post_signed_order
        Based on https://bluefin-exchange.readme.io/reference/sign-post-orders
        """
        logger.info(f"[MOCK] Posting signed order to exchange")
        order_id = f"order_{get_timestamp()}"
        
        # Create order response
        order = {
            "id": order_id,
            "orderHash": signed_order["orderHash"],
            "symbol": signed_order["symbol"],
            "side": signed_order["side"],
            "size": signed_order["size"],
            "price": signed_order["price"],
            "orderType": signed_order["orderType"],
            "leverage": signed_order["leverage"],
            "status": "OPEN",
            "timestamp": get_timestamp()
        }
        
        # Store order
        self.orders.append(order)
        
        return order
    
    async def create_order(self, symbol, side, size, **kwargs):
        """Mock implementation of create_order - now using the signature flow
        Based on https://bluefin-exchange.readme.io/reference/sign-post-orders
        """
        try:
            # Create order signature request
            signature_request = self.create_order_signature_request(
                symbol=symbol,
                side=side,
                size=size,
                price=kwargs.get("price", None),
                order_type=kwargs.get("type", "MARKET"),
                leverage=kwargs.get("leverage", 5)
            )
            
            # Create signed order
            signed_order = self.create_signed_order(signature_request)
            
            # Post signed order
            order = await self.post_signed_order(signed_order)
            
            logger.info(f"[MOCK] Created order: {order}")
            return order
        except Exception as e:
            logger.error(f"[MOCK] Error creating order: {e}")
            raise
    
    async def get_orders(self):
        """Mock implementation of get_orders"""
        logger.info(f"[MOCK] Getting orders, count: {len(self.orders)}")
        return self.orders
    
    async def cancel_order(self, order_id=None, order_hash=None):
        """Mock implementation of cancel_order
        Based on https://bluefin-exchange.readme.io/reference/sign-post-orders
        """
        logger.info(f"[MOCK] Cancelling order: {order_id or order_hash}")
        
        # Find order to cancel
        for i, order in enumerate(self.orders):
            if (order_id and order["id"] == order_id) or (order_hash and order["orderHash"] == order_hash):
                # Remove from orders list
                cancelled_order = self.orders.pop(i)
                cancelled_order["status"] = "CANCELLED"
                return {"success": True, "order": cancelled_order}
        
        return {"success": False, "error": "Order not found"}

# Define mock client for testing if no libraries are available
if BluefinClient is None:
    class BluefinClient:
        def __init__(self, *args, **kwargs):
            self.address = "0xmock_address"
            self.network = kwargs.get('network', 'testnet')
            self.api_key = kwargs.get('api_key', 'mock_api_key')
            self.api = self.MockAPI()
        
        class MockAPI:
            async def close_session(self):
                print("Mock: Closing session")
                
        async def init(self, *args, **kwargs):
            print("Mock: Initializing client")
            return self
            
        def get_public_address(self):
            return self.address
            
        async def connect(self):
            print("Mock: Connecting to Bluefin")
            return True
            
        async def disconnect(self):
            print("Mock: Disconnecting from Bluefin")
            return True
            
        async def get_user_account_data(self):
            print("Mock: Getting user account data")
            return {"balance": 1000.0}
            
        async def get_user_margin(self):
            print("Mock: Getting user margin")
            return {"available": 800.0}
            
        async def get_user_positions(self):
            print("Mock: Getting user positions")
            return []
            
        async def get_user_leverage(self, symbol):
            print(f"Mock: Getting user leverage for {symbol}")
            return 5
            
        def create_signed_order(self, signature_request):
            print("Mock: Creating signed order")
            return {"signature": "0xmock_signature"}
            
        async def post_signed_order(self, signed_order):
            print("Mock: Posting signed order")
            return {"orderId": "mock_order_id"}

        async def get_account_info(self):
            print("Mock: Getting account info")
            return {
                "address": self.address,
                "network": self.network,
                "balance": 1000.0,
                "available_margin": 800.0,
                "positions": []
            }
            
        async def place_order(self, **kwargs):
            print(f"Mock: Placing {kwargs.get('side')} order")
            return {"orderId": "mock_order_id"}

# Define a mock OrderSignatureRequest class for simulation
class MockOrderSignatureRequest:
    """Mock implementation of order signature request"""
    
    def __init__(self, symbol, side, size, price=None, order_type="MARKET", leverage=5, **kwargs):
        self.symbol = symbol
        self.side = side
        self.size = size
        self.price = price if price is not None else 0.0
        self.order_type = order_type
        self.leverage = leverage
        self.timestamp = int(time.time() * 1000)
        self.expiration = self.timestamp + 60000  # 1 minute expiration
        self.kwargs = kwargs
        
    def get_signature_hash(self):
        """Get the signature hash for the order"""
        # In a real implementation, this would create a hash of the order parameters
        # For mock purposes, we'll just create a unique string
        return f"0xSIGHASH_{self.symbol}_{self.side}_{self.size}_{self.timestamp}"
        
    def get_order_hash(self):
        """Get the order hash"""
        # In a real implementation, this would be a hash of the order parameters
        # For mock purposes, we'll just create a unique string
        return f"0xORDERHASH_{self.symbol}_{self.side}_{self.size}_{self.timestamp}"

# Set OrderSignatureRequest to the mock class by default
OrderSignatureRequest = MockOrderSignatureRequest

def initialize_risk_manager():
    """Initialize the risk management system."""
    logger.info("Initializing risk manager")
    # Here we would normally import and initialize a proper risk manager
    # For now, we'll just return the risk parameters
    logger.info(f"Risk parameters: {RISK_PARAMS}")
    return RISK_PARAMS

# Add a simple RiskManager class to replace references to the risk_manager module
class RiskManager:
    def __init__(self, risk_params):
        self.account_balance = risk_params.get("initial_account_balance", 1000)
        self.max_risk_per_trade = risk_params.get("risk_per_trade", 0.01)
        self.max_open_trades = risk_params.get("max_positions", 3)
        self.max_daily_drawdown = risk_params.get("max_daily_drawdown", 0.05)
        self.daily_pnl = 0
        
    def update_account_balance(self, balance):
        self.account_balance = balance
        
    def calculate_position_size(self, entry_price, stop_loss):
        risk_amount = self.account_balance * self.max_risk_per_trade
        price_risk = abs(entry_price - stop_loss)
        if price_risk == 0:
            return 0
        return risk_amount / price_risk
        
    def can_open_new_trade(self):
        # Check if we have too many open positions
        if self.current_positions >= self.max_open_trades:
            return False
            
        # Check if we've hit our daily drawdown limit
        if self.daily_pnl <= -self.account_balance * self.max_daily_drawdown:
            return False
            
        return True
        
    @property
    def current_positions(self):
        # This would normally check the actual positions
        # For now, just return a placeholder value
        return 0

# Initialize the risk manager
risk_manager = RiskManager(RISK_PARAMS)

# Add retry decorator for API calls
@backoff.on_exception(backoff.expo, 
                     (asyncio.TimeoutError, ConnectionError, OSError),
                     max_tries=3,
                     max_time=30)
async def get_account_info(client):
    """
    Retrieve account information and balances from Bluefin API.
    
    Note: The specific method calls and response format will depend on whether
    you're using the bluefin_client_sui library or bluefin.v2.client.
    
    For bluefin_client_sui:
    - Use client.get_user_account_data() for account data
    - Use client.get_user_margin() for margin data
    - Use client.get_user_positions() for positions
    
    For bluefin.v2.client:
    - The API structure is slightly different; refer to its documentation
    """
    try:
        # Get account data based on API
        if hasattr(client, 'get_user_account_data'):
            # bluefin_client_sui approach
            account_data = await client.get_user_account_data()
            margin_data = await client.get_user_margin()
            positions = await client.get_user_positions() or []
            
            account_info = {
                "balance": float(account_data.get("totalCollateralValue", 0)),
                "availableMargin": float(margin_data.get("availableMargin", 0)),
                "positions": positions
            }
        else:
            # Fallback for other client implementations
            account_info = await client.get_account_info()
            
        logger.info(f"Account info retrieved: balance={account_info['balance']}, "
                   f"margin={account_info['availableMargin']}, "
                   f"positions={len(account_info['positions'])}")
        
        return account_info
    except Exception as e:
        logger.error(f"Failed to retrieve account info: {e}")
        # Re-raise the exception to trigger the retry mechanism
        raise

# Default trading parameters
DEFAULT_PARAMS = {
    "symbol": "SUI/USD",
    "timeframe": "5m", 
    "leverage": 7,
    "stop_loss_pct": 0.15,
    "position_size_pct": 0.05,
    "max_positions": 3
}

# Symbol-specific parameters (can be overridden by user)
SYMBOL_PARAMS = {
    "SUI/USD": DEFAULT_PARAMS,
    "BTC/USD": {
        "symbol": "BTC/USD",
        "timeframe": "15m",
        "leverage": 10,
        "stop_loss_pct": 0.1,
        "position_size_pct": 0.03,
        "max_positions": 2
    },
    "ETH/USD": {
        "symbol": "ETH/USD", 
        "timeframe": "15m",
        "leverage": 8,
        "stop_loss_pct": 0.12,
        "position_size_pct": 0.04,
        "max_positions": 2
    }
}

# Add functions for client initialization
def init_bluefin_client():
    """
    Initialize the Bluefin client for trading.
    
    This function checks for the availability of the Bluefin client libraries
    and initializes the appropriate client based on environment variables:
    
    - For SUI-based client: BLUEFIN_PRIVATE_KEY and BLUEFIN_NETWORK
    - For API-based client: BLUEFIN_API_KEY and BLUEFIN_API_SECRET
    
    If no environment variables are set or there's an error, it falls back to a mock client.
    
    Returns:
        client: The initialized Bluefin client
    """
    global client, MOCK_TRADING
    
    try:
        # Check if we should use mock trading
        mock_trading_env = os.getenv("MOCK_TRADING", "false").lower()
        MOCK_TRADING = mock_trading_env in ["true", "1", "yes"]
        
        if MOCK_TRADING:
            logger.info("Mock trading enabled, using MockBluefinClient")
            return MockBluefinClient()
            
        # Check for SUI-based client
        if BLUEFIN_CLIENT_SUI_AVAILABLE and os.getenv("BLUEFIN_PRIVATE_KEY"):
            try:
                # Get network configuration
                network_name = os.getenv("BLUEFIN_NETWORK", "SUI_PROD")
                
                # Determine network value
                network_value = None
                if Networks is not None:
                    if hasattr(Networks, network_name):
                        network_value = getattr(Networks, network_name)
                    elif network_name in ["mainnet", "testnet", "MAINNET", "TESTNET"]:
                        network_value = getattr(Networks, network_name.upper(), None)
                
                if network_value is None:
                    logger.warning(f"Network {network_name} not found, using SUI_PROD as default")
                    network_value = Networks.SUI_PROD
                
                logger.info(f"Initializing Bluefin SUI client with network: {network_name}")
                
                # Initialize the SUI-based client
                client = BluefinClient(
                    are_terms_accepted=True,
                    network=network_value,
                    private_key=os.getenv("BLUEFIN_PRIVATE_KEY")
                )
                
                # Initialize the client asynchronously
                asyncio.create_task(client.init(onboard_user=True))
                
                logger.info("Bluefin SUI client initialized successfully")
                return client
                
            except Exception as e:
                logger.error(f"Error initializing Bluefin SUI client: {e}")
                logger.exception(e)
                
        # Check for API-based client
        if BLUEFIN_V2_CLIENT_AVAILABLE and os.getenv("BLUEFIN_API_KEY") and os.getenv("BLUEFIN_API_SECRET"):
            try:
                logger.info("Initializing Bluefin API client")
                
                # Initialize the API-based client
                client = BluefinClient(
                    api_key=os.getenv("BLUEFIN_API_KEY"),
                    api_secret=os.getenv("BLUEFIN_API_SECRET"),
                    use_testnet=os.getenv("BLUEFIN_TESTNET", "false").lower() in ["true", "1", "yes"]
                )
                
                logger.info("Bluefin API client initialized successfully")
                return client
                
            except Exception as e:
                logger.error(f"Error initializing Bluefin API client: {e}")
                logger.exception(e)
        
        # If no client could be initialized, use mock client
        logger.warning("No Bluefin client configuration found, using MockBluefinClient")
        return MockBluefinClient()
        
    except Exception as e:
        logger.error(f"Error in init_bluefin_client: {e}")
        logger.exception(e)
        
        # Return a mock client as fallback
        return MockBluefinClient()

def init_claude_client():
    """Initialize the Claude API client using environment variables"""
    global claude_client
    
    try:
        # Check if Claude is available
        if not CLAUDE_AVAILABLE:
            logger.warning("Claude API not available - anthropic package not installed")
            return None
            
        # Check for API key in environment variables
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        
        if not api_key or api_key == "your_api_key_here":
            logger.warning("Claude API key not found or not set in environment variables")
            return None
        
        # Initialize Claude client with API key
        logger.info("Initializing Claude client with Anthropic API key")
        claude_client = Client(api_key=api_key, max_retries=3)
        
        return claude_client
    except Exception as e:
        logger.error(f"Failed to initialize Claude client: {e}")
        return None

async def init_clients():
    """Initialize API clients"""
    # Define a global client for the whole application
    global client, claude_client
    
    # Initialize Bluefin client
    logger.info("Initializing Bluefin client")
    client = init_bluefin_client()
    
    # Initialize the Bluefin client if it's not a mock client
    if not isinstance(client, MockBluefinClient) and not MOCK_TRADING:
        try:
            # According to https://bluefin-exchange.readme.io/reference/initialization
            # The client needs to be initialized with await client.init()
            logger.info("Initializing real Bluefin client connection...")
            await client.init(onboard_user=True)
            logger.info("Bluefin client initialized successfully")
            
            # Get and log the public address
            public_address = client.get_public_address()
            logger.info(f"Connected with wallet address: {public_address}")
            
            # Get account details
            account_details = await client.get_account_info()
            logger.info(f"Account details: {account_details}")
        except Exception as e:
            logger.error(f"Error initializing Bluefin client: {e}")
            logger.error(traceback.format_exc())
            logger.warning("Falling back to mock client")
            client = MockBluefinClient()
    
    # Initialize Claude client 
    logger.info("Initializing Claude client")
    claude_client = init_claude_client()
    
    return client

def get_timestamp():
    """Get current timestamp in YYYYMMDD_HHMMSS format"""
    return datetime.now().strftime("%Y%m%d_%H%M%S")

def opposite_type(order_type: str) -> str:
    """Get the opposite order type (BUY -> SELL, SELL -> BUY)"""
    return "BUY" if order_type == "SELL" else "SELL"

def capture_chart_screenshot(ticker, timeframe="1D"):
    """Capture a screenshot of the TradingView chart for the given ticker and timeframe"""
    # Check if Playwright is available
    if not PLAYWRIGHT_AVAILABLE or sync_playwright is None:
        logger.error("Playwright is not available. Cannot capture chart screenshot.")
        return None
        
    try:
        with sync_playwright() as p:
            # Create screenshots directory if it doesn't exist
            os.makedirs("screenshots", exist_ok=True)
            
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            
            # Navigate to TradingView chart for the specified ticker
            page.goto(f"https://www.tradingview.com/chart/?symbol={ticker}")
            
            # Wait for chart to load completely
            page.wait_for_selector(".chart-container")
            
            # Take screenshot
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            screenshot_path = f"screenshots/{ticker}_{timeframe}_{timestamp}.png"
            page.screenshot(path=screenshot_path)
            browser.close()
            
            return screenshot_path
    except Exception as e:
        logger.error(f"Error capturing chart screenshot: {e}")
        return None

def analyze_chart_with_perplexity(screenshot_path, ticker):
    """Analyze a chart screenshot using Perplexity AI"""
    # Get API key from environment
    api_key = os.environ.get("PERPLEXITY_API_KEY")
    if not api_key:
        logger.error("Perplexity API key not found in environment variables")
        return None
    
    # Construct a simple text-only prompt for testing
    prompt = {
        "model": "sonar",
        "messages": [
            {
                "role": "user",
                "content": f"Analyze the current market conditions for {ticker}. Would you recommend a BUY, SELL, or HOLD position? Include your reasoning."
            }
        ],
        "max_tokens": 1000
    }
    
    # Setup headers
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    
    # Send to Perplexity API
    response = requests.post("https://api.perplexity.ai/chat/completions", json=prompt, headers=headers)
    
    # Process response
    if response.status_code == 200:
        analysis = response.json()
        # Debug: Print the raw response
        logger.info(f"Raw Perplexity response: {json.dumps(analysis, indent=2)}")
        return analysis
    else:
        logger.error(f"Error from Perplexity API: {response.status_code} - {response.text}")
        return None

async def analyze_chart_with_claude(screenshot_path, ticker):
    """
    Analyze a chart screenshot using Claude AI
    
    Args:
        screenshot_path: Path to the screenshot image
        ticker: Symbol being analyzed
        
    Returns:
        dict: Analysis results with trading recommendations
    """
    global claude_client
    
    if not claude_client:
        logger.error("Claude client not initialized, cannot analyze chart")
        return {"error": "Claude client not initialized"}
        
    try:
        # Load config settings
        try:
            from config import CLAUDE_CONFIG
            max_tokens = CLAUDE_CONFIG.get("max_tokens", 8000)
            model = CLAUDE_CONFIG.get("model", "claude-3.7-sonnet")
            temperature = CLAUDE_CONFIG.get("temperature", 0.2)
        except ImportError:
            logger.warning("Could not import CLAUDE_CONFIG, using defaults")
            max_tokens = int(os.getenv("CLAUDE_MAX_TOKENS", 8000))
            model = os.getenv("CLAUDE_MODEL", "claude-3.7-sonnet")
            temperature = float(os.getenv("CLAUDE_TEMPERATURE", 0.2))
        
        # Check if screenshot exists
        if not os.path.exists(screenshot_path):
            logger.error(f"Screenshot not found at {screenshot_path}")
            return {"error": f"Screenshot not found at {screenshot_path}"}
            
        # Convert image to base64 for transmission
        with open(screenshot_path, "rb") as image_file:
            encoded_image = base64.b64encode(image_file.read()).decode('utf-8')
        
        # Construct system prompt
        system_prompt = f"""You are an expert cryptocurrency trader and technical analyst.
You are analyzing a trading chart for {ticker} to make trading decisions.
Analyze the chart thoroughly and provide:
1. Key technical indicators visible on the chart
2. Support and resistance levels
3. Current market trend (bullish, bearish, or neutral)
4. Trading recommendation (BUY, SELL, or HOLD) with specific entry, stop loss, and take profit levels
5. Confidence score (1-10) for your recommendation
6. Risk/reward ratio for the recommended trade

Format your analysis in a structured way with clear sections."""
        
        # Make API call to Claude
        logger.info(f"Sending chart analysis request to Claude for {ticker}")
        
        # Create message with anthropic.Client - using correct schema
        response = claude_client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system_prompt,
            messages=[
                {
                    "role": "user", 
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": encoded_image
                            }
                        },
                        {
                            "type": "text",
                            "text": f"Analyze this {ticker} chart and provide a detailed trading recommendation."
                        }
                    ]
                }
            ]
        )
        
        # Extract text from response safely
        analysis_text = ""
        
        # Handle different possible response structures
        try:
            # If response is an object with content attribute
            if hasattr(response, "content"):
                content = response.content
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and "text" in block:
                            analysis_text += block["text"]
                        elif hasattr(block, "type") and getattr(block, "type", "") == "text":
                            # Use getattr with default to avoid attribute errors
                            text = getattr(block, "text", "")
                            if text:
                                analysis_text += text
                        elif isinstance(block, str):
                            analysis_text += block
            # If response is a dictionary
            elif isinstance(response, dict) and "content" in response:
                content = response["content"]
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and "text" in block:
                            analysis_text += block["text"]
                elif isinstance(content, str):
                    analysis_text = content
                    
            # If no text extracted but we have a response, use string representation as fallback
            if not analysis_text and response:
                analysis_text = str(response)
        except Exception as e:
            logger.error(f"Error parsing Claude response: {str(e)}")
            # Fallback to string representation
            analysis_text = str(response)
                
        if not analysis_text:
            logger.error("No text extracted from Claude response")
            return {"error": "Failed to extract text from Claude response"}
            
        # Parse the analysis to extract trading recommendation
        trading_analysis = parse_claude_analysis(analysis_text, ticker)
        
        return trading_analysis
            
    except Exception as e:
        logger.error(f"Error in Claude chart analysis: {str(e)}")
        return {"error": f"Claude analysis error: {str(e)}"}

def parse_claude_analysis(analysis_text, ticker):
    """
    Parse Claude's analysis to extract trading recommendations
    
    Args:
        analysis_text: Raw analysis text from Claude
        ticker: Symbol being analyzed
        
    Returns:
        dict: Structured trading recommendation
    """
    # Default values
    recommendation = {
        "symbol": ticker,
        "action": "NONE",  # Default to no action
        "entry_price": None,
        "stop_loss": None,
        "take_profit": None,
        "confidence": 0,  # 0-10 scale
        "risk_reward_ratio": 0,
        "trend": "NEUTRAL"
    }
    
    try:
        # Extract action (BUY/SELL/HOLD)
        if "BUY" in analysis_text.upper() or "LONG" in analysis_text.upper():
            recommendation["action"] = "BUY"
        elif "SELL" in analysis_text.upper() or "SHORT" in analysis_text.upper():
            recommendation["action"] = "SELL"
        elif "HOLD" in analysis_text.upper() or "NEUTRAL" in analysis_text.upper():
            recommendation["action"] = "NONE"
            
        # Extract trend
        if "BULLISH" in analysis_text.upper():
            recommendation["trend"] = "BULLISH"
        elif "BEARISH" in analysis_text.upper():
            recommendation["trend"] = "BEARISH"
            
        # Extract confidence score (1-10)
        confidence_match = re.search(r"confidence[:\s]+(\d+)(?:\s*\/\s*10)?", analysis_text.lower())
        if confidence_match:
            recommendation["confidence"] = int(confidence_match.group(1))
            
        # Extract price levels (using regex)
        # Entry price
        entry_match = re.search(r"entry[:\s]+[$]?(\d+(?:\.\d+)?)", analysis_text.lower())
        if entry_match:
            recommendation["entry_price"] = float(entry_match.group(1))
            
        # Stop loss
        sl_match = re.search(r"stop[:\s]*loss[:\s]+[$]?(\d+(?:\.\d+)?)", analysis_text.lower())
        if sl_match:
            recommendation["stop_loss"] = float(sl_match.group(1))
            
        # Take profit
        tp_match = re.search(r"take[:\s]*profit[:\s]+[$]?(\d+(?:\.\d+)?)", analysis_text.lower())
        if tp_match:
            recommendation["take_profit"] = float(tp_match.group(1))
            
        # Risk/reward ratio
        rr_match = re.search(r"risk[:/]reward[:\s]+(\d+(?:\.\d+)?)[:\s]*(?:to)[:\s]*(\d+(?:\.\d+)?)", analysis_text.lower())
        if rr_match:
            reward = float(rr_match.group(2))
            risk = float(rr_match.group(1))
            if risk > 0:
                recommendation["risk_reward_ratio"] = reward / risk
                
    except Exception as e:
        logger.error(f"Error parsing Claude analysis: {e}")
        
    return recommendation

async def execute_trade_when_appropriate(analysis):
    """Execute a trade if the analysis recommends it with sufficient confidence"""
    if not analysis or not isinstance(analysis, dict):
        logger.warning("Invalid analysis data, cannot execute trade")
        return
        
    trade_rec = analysis.get("recommendation", {})
    action = trade_rec.get("action", "NONE")
    confidence = trade_rec.get("confidence", 0)
    
    # Default min confidence
    min_confidence = 0.7
    
    # Get min_confidence from TRADING_PARAMS if available
    if 'TRADING_PARAMS' in globals() and isinstance(TRADING_PARAMS, dict):
        min_confidence = TRADING_PARAMS.get("min_confidence", min_confidence)
    
    if action != "NONE" and confidence >= min_confidence:
        logger.info(f"Executing {action} trade with confidence {confidence}")
        # Execute trade logic here
    else:
        logger.info(f"Not executing trade. Action: {action}, Confidence: {confidence}")

def parse_perplexity_analysis(analysis, ticker):
    """
    Parse Perplexity API response to extract trading recommendations
    
    Args:
        analysis: The raw Perplexity API response
        ticker: The symbol being analyzed
        
    Returns:
        dict: Trading recommendation with action, confidence, and other metrics
    """
    recommendation = {
        "symbol": ticker,
        "timestamp": datetime.now().isoformat(),
        "recommendation": {
            "action": "NONE",
            "confidence": 0,
            "entry_price": None,
            "stop_loss": None,
            "take_profit": None,
            "risk_reward_ratio": None,
            "timeframe": None
        }
    }
    
    try:
        if not analysis or not isinstance(analysis, dict):
            logger.warning("Invalid Perplexity analysis data")
            return recommendation
            
        # Extract the response text from Perplexity
        analysis_text = ""
        if "choices" in analysis and len(analysis["choices"]) > 0:
            message = analysis["choices"][0].get("message", {})
            if "content" in message:
                analysis_text = message["content"]
            
        if not analysis_text:
            logger.warning("No text content found in Perplexity analysis")
            return recommendation
            
        # Debug: Print the extracted text
        logger.info(f"Extracted analysis text: {analysis_text[:200]}...")
        
        # Detect recommendation type based on explicit statements
        recommendation_type = "NONE"
        confidence = 0.0
        
        # Look for explicit recommendations
        if re.search(r'recommendation.*?\b(buy|long)\b', analysis_text.lower()) or re.search(r'\b(buy|long)\b.*?recommended', analysis_text.lower()):
            recommendation_type = "BUY"
            confidence = 0.8
        elif re.search(r'recommendation.*?\b(sell|short)\b', analysis_text.lower()) or re.search(r'\b(sell|short)\b.*?recommended', analysis_text.lower()):
            recommendation_type = "SELL"
            confidence = 0.8
        elif re.search(r'recommendation.*?\b(hold|neutral|accumulate)\b', analysis_text.lower()) or re.search(r'\b(hold|neutral|accumulate)\b.*?recommended', analysis_text.lower()):
            recommendation_type = "HOLD"
            confidence = 0.7
            
        # If no explicit recommendation, use sentiment analysis
        if recommendation_type == "NONE":
            # Look for buy/sell signals
            buy_indicators = ["buy", "bullish", "uptrend", "long", "positive", "increase", "growth"]
            sell_indicators = ["sell", "bearish", "downtrend", "short", "negative", "decrease", "fall"]
            hold_indicators = ["hold", "neutral", "mixed", "cautious", "moderate", "balanced", "sideways", "accumulate"]
            
            # Count mentions of bullish/bearish terms
            buy_count = sum(1 for indicator in buy_indicators if indicator in analysis_text.lower())
            sell_count = sum(1 for indicator in sell_indicators if indicator in analysis_text.lower())
            hold_count = sum(1 for indicator in hold_indicators if indicator in analysis_text.lower())
            
            # Determine action based on sentiment
            if buy_count > sell_count + hold_count:
                recommendation_type = "BUY"
                confidence = min(0.5 + (buy_count - sell_count) * 0.05, 0.75)
            elif sell_count > buy_count + hold_count:
                recommendation_type = "SELL"
                confidence = min(0.5 + (sell_count - buy_count) * 0.05, 0.75)
            elif hold_count > 0:
                recommendation_type = "HOLD"
                confidence = min(0.5 + hold_count * 0.05, 0.7)
        
        recommendation["recommendation"]["action"] = recommendation_type
        recommendation["recommendation"]["confidence"] = confidence
            
        # Extract price targets if available
        price_match = re.search(r"(?:current|price|trading at)[:\s]+\$?(\d+(?:\.\d+)?)", analysis_text.lower())
        if price_match:
            recommendation["recommendation"]["entry_price"] = float(price_match.group(1))
            
        # Look for support levels as potential stop loss
        sl_match = re.search(r"(?:stop[- ]loss|support)[:\s]+\$?(\d+(?:\.\d+)?)", analysis_text.lower())
        if sl_match:
            recommendation["recommendation"]["stop_loss"] = float(sl_match.group(1))
            
        # Look for resistance as potential take profit
        tp_match = re.search(r"(?:take[- ]profit|target|resistance)[:\s]+\$?(\d+(?:\.\d+)?)", analysis_text.lower())
        if tp_match:
            recommendation["recommendation"]["take_profit"] = float(tp_match.group(1))
            
        # Try to extract timeframe
        if "short-term" in analysis_text.lower() or "day" in analysis_text.lower() or "hourly" in analysis_text.lower():
            recommendation["recommendation"]["timeframe"] = "short-term"
        elif "medium-term" in analysis_text.lower() or "week" in analysis_text.lower() or "monthly" in analysis_text.lower():
            recommendation["recommendation"]["timeframe"] = "medium-term"
        elif "long-term" in analysis_text.lower() or "year" in analysis_text.lower():
            recommendation["recommendation"]["timeframe"] = "long-term"
            
        # Calculate risk/reward if both stop-loss and take-profit are available
        if recommendation["recommendation"]["stop_loss"] and recommendation["recommendation"]["take_profit"] and recommendation["recommendation"]["entry_price"]:
            entry = recommendation["recommendation"]["entry_price"]
            sl = recommendation["recommendation"]["stop_loss"]
            tp = recommendation["recommendation"]["take_profit"]
            
            if recommendation["recommendation"]["action"] == "BUY":
                if entry > sl and tp > entry:  # Valid buy setup
                    risk = entry - sl
                    reward = tp - entry
                    if risk > 0:
                        recommendation["recommendation"]["risk_reward_ratio"] = reward / risk
            elif recommendation["recommendation"]["action"] == "SELL":
                if entry < sl and tp < entry:  # Valid sell setup
                    risk = sl - entry
                    reward = entry - tp
                    if risk > 0:
                        recommendation["recommendation"]["risk_reward_ratio"] = reward / risk
    
    except Exception as e:
        logger.error(f"Error parsing Perplexity analysis: {e}")
        
    return recommendation

async def execute_trade(symbol: str, side: str, position_size: float = None, risk_percentage: float = None, stop_loss_percentage: float = None, take_profit_percentage: float = None, leverage: int = None, order_type: str = "MARKET", price: float = None):
    """
    Execute a real trade on the Bluefin exchange.
    
    This function places an order on Bluefin based on the provided parameters:
    - symbol: The trading pair to trade (e.g., "SUI-PERP")
    - side: The direction of the trade ("BUY" or "SELL")
    - position_size: The size of the position to open (optional, will be calculated if not provided)
    - risk_percentage: The percentage of account to risk (optional)
    - stop_loss_percentage: The percentage for stop loss (optional)
    - take_profit_percentage: The percentage for take profit (optional)
    - leverage: The leverage to use for the trade (optional, default from config)
    - order_type: The type of order ("MARKET" or "LIMIT")
    - price: The price for limit orders (required for LIMIT orders)
    
    It follows the Bluefin order flow:
    1. Get the current oracle price from Bluefin API
    2. Create an order signature request using that price
    3. Sign the order
    4. Post the signed order to the exchange
    5. Calculate and place a stop loss order based on the environment variable
    
    Returns:
        dict: The order response from Bluefin (real or mock)
    """
    global client
    
    try:
        # Set default values from environment variables if not provided
        if position_size is None and os.getenv("DEFAULT_POSITION_SIZE_PCT"):
            position_size = float(os.getenv("DEFAULT_POSITION_SIZE_PCT", 0.05))
        
        if risk_percentage is None:
            risk_percentage = float(os.getenv("DEFAULT_RISK_PERCENTAGE", 0.02))
        
        if stop_loss_percentage is None:
            stop_loss_percentage = float(os.getenv("DEFAULT_STOP_LOSS_PERCENTAGE", 0.05))
        
        if take_profit_percentage is None and os.getenv("DEFAULT_TAKE_PROFIT_PCT"):
            take_profit_percentage = float(os.getenv("DEFAULT_TAKE_PROFIT_PCT", 0.3))
        
        if leverage is None and os.getenv("DEFAULT_LEVERAGE"):
            leverage = int(os.getenv("DEFAULT_LEVERAGE", 5))
            
        # Check if client is initialized
        if client is None:
            logger.error("Trade client not initialized")
            return None
        
        # Calculate position size if not provided
        if position_size is None:
            position_size = await calculate_position_size(
                symbol=symbol,
                side=side,
                risk_percentage=risk_percentage,
                stop_loss_percentage=stop_loss_percentage
            )
            
        logger.info(f"Executing trade: {side} {position_size} of {symbol} with order type {order_type}")
        
        # Get parameters for symbol
        leverage_value = leverage or int(os.getenv("DEFAULT_LEVERAGE", "5"))
        
        # Ensure leverage is set correctly
        await ensure_leverage(symbol, leverage_value)
        
        # Get current market price from Bluefin exchange API
        logger.info(f"Getting current market price for {symbol} from Bluefin exchange")
        market_price = await get_market_price(symbol)
        logger.info(f"Current market price for {symbol}: {market_price}")
        
        # For LIMIT orders, use the current market price if none provided
        if order_type == "LIMIT" and price is None:
            price = market_price
            logger.info(f"Setting limit price to current market price: {price}")
        
        # Create order using the signature flow
        try:
            # Check if client supports the signature flow
            if hasattr(client, "create_order_signature_request") and hasattr(client, "create_signed_order") and hasattr(client, "post_signed_order"):
                # Step 1: Create order signature request
                signature_request = client.create_order_signature_request(
                    symbol=symbol,
                    side=side,
                    size=position_size,
                    price=price,
                    order_type=order_type,
                    leverage=leverage_value
                )
                logger.info(f"Created order signature request")
                
                # Step 2: Sign the order
                signed_order = client.create_signed_order(signature_request)
                logger.info(f"Created signed order: {signed_order}")
                
                # Step 3: Post the signed order
                main_order = await client.post_signed_order(signed_order)
                logger.info(f"Posted signed order, response: {main_order}")
            else:
                # Fallback to direct order placement if signature flow not supported
                main_order = await client.place_order(
                    symbol=symbol,
                    side=side,
                    quantity=position_size,
                    price=price,
                    order_type=order_type,
                    leverage=leverage_value
                )
                logger.info(f"Placed order directly, response: {main_order}")
            
            # Place stop loss order using STOP_MARKET type
            if stop_loss_percentage and stop_loss_percentage > 0 and main_order:
                try:
                    # Use the actual execution price or market price to calculate stop loss
                    entry_price = main_order.get("price") if main_order.get("price") else market_price
                    
                    # Calculate stop loss price based on entry price and direction
                    if side == ORDER_SIDE.BUY:
                        # For long positions, stop loss is below entry price
                        stop_price = entry_price * (1 - stop_loss_percentage)
                    else:
                        # For short positions, stop loss is above entry price
                        stop_price = entry_price * (1 + stop_loss_percentage)
                    
                    # Place stop loss order
                    stop_loss_side = ORDER_SIDE.SELL if side == ORDER_SIDE.BUY else ORDER_SIDE.BUY
                    
                    logger.info(f"Placing stop loss order at {stop_price} for {position_size} {symbol}")
                    
                    if hasattr(client, "create_order_signature_request"):
                        # Use signature flow for stop loss
                        sl_signature_request = client.create_order_signature_request(
                            symbol=symbol,
                            side=stop_loss_side,
                            size=position_size,
                            price=stop_price,
                            order_type="STOP_MARKET",
                            reduce_only=True,
                            leverage=leverage_value
                        )
                        sl_signed_order = client.create_signed_order(sl_signature_request)
                        sl_order = await client.post_signed_order(sl_signed_order)
                        logger.info(f"Placed stop loss order, response: {sl_order}")
                    else:
                        # Use direct order placement
                        sl_order = await client.place_order(
                            symbol=symbol,
                            side=stop_loss_side,
                            quantity=position_size,
                            price=stop_price,
                            order_type="STOP_MARKET",
                            reduce_only=True,
                            leverage=leverage_value
                        )
                        logger.info(f"Placed stop loss order, response: {sl_order}")
                except Exception as e:
                    logger.error(f"Error placing stop loss order: {e}", exc_info=True)
            
            # Place take profit order if percentage is provided
            if take_profit_percentage and take_profit_percentage > 0 and main_order:
                try:
                    # Use the actual execution price or market price to calculate take profit
                    entry_price = main_order.get("price") if main_order.get("price") else market_price
                    
                    if side == ORDER_SIDE.BUY:
                        # For long positions, take profit is above entry price
                        take_profit_price = entry_price * (1 + take_profit_percentage)
                    else:
                        # For short positions, take profit is below entry price
                        take_profit_price = entry_price * (1 - take_profit_percentage)
                    
                    # Place take profit order
                    take_profit_side = ORDER_SIDE.SELL if side == ORDER_SIDE.BUY else ORDER_SIDE.BUY
                    
                    logger.info(f"Placing take profit order at {take_profit_price} for {position_size} {symbol}")
                    
                    if hasattr(client, "create_order_signature_request"):
                        # Use signature flow for take profit
                        tp_signature_request = client.create_order_signature_request(
                            symbol=symbol,
                            side=take_profit_side,
                            size=position_size,
                            price=take_profit_price,
                            order_type="LIMIT",
                            reduce_only=True,
                            leverage=leverage_value
                        )
                        tp_signed_order = client.create_signed_order(tp_signature_request)
                        tp_order = await client.post_signed_order(tp_signed_order)
                        logger.info(f"Placed take profit order, response: {tp_order}")
                    else:
                        # Use direct order placement
                        tp_order = await client.place_order(
                            symbol=symbol,
                            side=take_profit_side,
                            quantity=position_size,
                            price=take_profit_price,
                            order_type="LIMIT",
                            reduce_only=True,
                            leverage=leverage_value
                        )
                        logger.info(f"Placed take profit order, response: {tp_order}")
                except Exception as e:
                    logger.error(f"Error placing take profit order: {e}", exc_info=True)
            
            return main_order
        except Exception as e:
            logger.error(f"Error executing trade: {e}", exc_info=True)
            return None
    except Exception as e:
        logger.error(f"Error in execute_trade: {e}", exc_info=True)
        return None

async def process_alerts():
    """
    Process incoming alerts from the webhook server.
    
    This function monitors the alerts directory for new JSON files containing trading signals.
    When a new alert is detected, it extracts the relevant data and determines the appropriate
    trading action based on the signal type and other parameters.
    
    For supported signal types (e.g., VuManChu Cipher B), it will execute a mock or real trade
    on the Bluefin exchange depending on the MOCK_TRADING setting.
    
    Unsupported alert types are logged and skipped.
    
    The processed alert files are deleted to avoid double-processing.
    """
    
    if not os.path.exists("alerts"):
        os.makedirs("alerts", exist_ok=True)
        return
        
    # Check for new alert files
    for file in os.listdir("alerts"):
        if file.endswith(".json"):
            alert_path = os.path.join("alerts", file)
            
            try:
                # Read the alert data
                with open(alert_path, "r") as f:
                    alert = json.load(f)
                
                logger.info(f"New alert received: {alert}")
                
                # Handle direct alert format from webhook server
                if "symbol" in alert and "type" in alert:
                    symbol = alert.get("symbol")
                    trade_type = alert.get("type")
                    position_size = alert.get("position_size", float(os.getenv("DEFAULT_POSITION_SIZE_PCT", "0.05")))
                    leverage = alert.get("leverage", int(os.getenv("DEFAULT_LEVERAGE", "5")))
                    stop_loss = alert.get("stop_loss", float(os.getenv("DEFAULT_STOP_LOSS_PCT", "0.15")))
                    take_profit = alert.get("take_profit", float(os.getenv("DEFAULT_TAKE_PROFIT_PCT", "0.3")))
                    
                    # Determine the order side
                    if trade_type.lower() == "buy":
                        side = ORDER_SIDE.BUY
                    elif trade_type.lower() == "sell":
                        side = ORDER_SIDE.SELL
                    else:
                        logger.warning(f"Invalid trade type in alert: {trade_type}")
                        os.remove(alert_path)
                        continue
                    
                    # Execute the trade
                    if MOCK_TRADING:
                        # Mock trade only - log the intent
                        logger.info(f"MOCK TRADE: Would execute a {side} trade for {symbol} with position size {position_size}, leverage {leverage}, stop loss {stop_loss}, take profit {take_profit}")
                    else:
                        # Execute real trade on Bluefin
                        try:
                            logger.info(f"Executing {side} trade for {symbol} with position size {position_size}, leverage {leverage}, stop loss {stop_loss}, take profit {take_profit}")
                            
                            # Ensure the leverage is set correctly
                            await ensure_leverage(symbol, leverage)
                            
                            # Execute the trade
                            await execute_trade(
                                symbol=symbol, 
                                side=side, 
                                position_size=position_size,
                                leverage=leverage,
                                stop_loss_percentage=stop_loss,
                                take_profit_percentage=take_profit
                            )
                        except Exception as e:
                            logger.error(f"Error executing trade: {e}", exc_info=True)
                
                # Extract key data from the original TradingView alert format
                elif "indicator" in alert and alert["indicator"] == "vmanchu_cipher_b":
                    symbol = alert.get("symbol", os.getenv("DEFAULT_SYMBOL", "SUI/USD"))
                    timeframe = alert.get("timeframe", os.getenv("DEFAULT_TIMEFRAME", "5m"))
                    signal_type = alert.get("signal_type", "")
                    action = alert.get("action", "")
                    
                    logger.info(f"Processing VuManChu Cipher B signal: {signal_type}")
                    logger.info(f"Symbol: {symbol}, Timeframe: {timeframe}, Action: {action}")
                    
                    # Map TradingView symbol to Bluefin format
                    if "/" in symbol:
                        base_currency = symbol.split("/")[0]
                        bluefin_symbol = f"{base_currency}-PERP"
                    else:
                        bluefin_symbol = f"{symbol}-PERP"
                    
                    # Determine trade direction based on signal type and action
                    if action == "BUY":
                        trade_direction = "Bullish"
                        side = ORDER_SIDE.BUY
                    elif action == "SELL":
                        trade_direction = "Bearish"
                        side = ORDER_SIDE.SELL
                    else:
                        logger.warning(f"Invalid action in alert: {action}")
                        os.remove(alert_path)
                        continue
                    
                    # Check if this is a valid signal type
                    valid_signals = ["GREEN_CIRCLE", "RED_CIRCLE", "GOLD_CIRCLE", "PURPLE_TRIANGLE"]
                    if signal_type not in valid_signals:
                        logger.warning(f"Invalid signal type: {signal_type}")
                        os.remove(alert_path)
                        continue
                    
                    # Execute trade based on the signal
                    if MOCK_TRADING:
                        # Mock trade only - log the intent
                        logger.info(f"MOCK TRADE: Would execute a {side} trade for {bluefin_symbol} based on {signal_type} signal")
                        logger.info(f"Trade direction: {trade_direction}")
                    else:
                        # Execute real trade on Bluefin
                        try:
                            position_size = float(os.getenv("DEFAULT_POSITION_SIZE_PCT", "0.05"))
                            leverage = int(os.getenv("DEFAULT_LEVERAGE", "5"))
                            stop_loss = float(os.getenv("DEFAULT_STOP_LOSS_PCT", "0.15"))
                            take_profit = float(os.getenv("DEFAULT_TAKE_PROFIT_PCT", "0.3"))
                            
                            logger.info(f"Executing {side} trade for {bluefin_symbol} with position size {position_size}, leverage {leverage}, stop loss {stop_loss}, take profit {take_profit}")
                            
                            # Ensure the leverage is set correctly
                            await ensure_leverage(bluefin_symbol, leverage)
                            
                            # Execute the trade
                            await execute_trade(
                                symbol=bluefin_symbol, 
                                side=side, 
                                position_size=position_size,
                                leverage=leverage,
                                stop_loss_percentage=stop_loss,
                                take_profit_percentage=take_profit
                            )
                        except Exception as e:
                            logger.error(f"Error executing trade: {e}", exc_info=True)
                else:
                    logger.warning(f"Unsupported alert format: {alert}")
                
                # Clean up the processed alert file
                os.remove(alert_path)
                
            except json.JSONDecodeError:
                logger.error(f"Error decoding JSON from file: {alert_path}")
                os.remove(alert_path)
            except Exception as e:
                logger.error(f"Error processing alert file {alert_path}: {e}", exc_info=True)
    
    # Small delay to avoid high CPU usage
    await asyncio.sleep(1)

# Define a main function for running the agent
async def main():
    setup_logging()
    logger.info("Starting agent...")
    
    # Create necessary directories
    os.makedirs("alerts", exist_ok=True)
    os.makedirs("logs", exist_ok=True)
    
    # Initialize clients
    await init_clients()
    
    # Start API server in the background
    api_task = asyncio.create_task(start_api_server())
    
    # Start alert processing loop
    while True:
        try:
            await process_alerts()
        except Exception as e:
            logger.error(f"Error processing alerts: {e}")
        await asyncio.sleep(1)

# Define FastAPI app
app = FastAPI(title="Trading Agent API", description="API for the trading agent")

@app.get("/")
async def root():
    return {"status": "online", "message": "Trading Agent API is running"}

@app.get("/health")
async def health_check():
    """Simple health check endpoint."""
    return {
        "status": "OK", 
        "timestamp": datetime.now().isoformat(),
        "version": "1.0.0"
    }

@app.get("/status")
async def get_status():
    """Get the current status of the trading agent."""
    # TODO: Return actual agent status
    return {
        "status": "running",
        "last_analysis": get_timestamp(),
        "open_positions": 2,
        "recent_trades": 5
    }

@app.get("/positions")
async def get_positions():
    """Get the list of open positions."""
    try:
        # Get positions from the client
        if client:
            if hasattr(client, "get_positions"):
                positions = await client.get_positions()
            elif hasattr(client, "get_account_details"):
                account_details = await client.get_account_details()
                positions = account_details.get("positions", [])
            else:
                logger.warning("Client does not have get_positions or get_account_details method")
                positions = []
                
            # Format positions for API response
            formatted_positions = []
            for pos in positions:
                formatted_pos = {
                    "id": pos.get("id", f"pos_{len(formatted_positions) + 1}"),
                    "symbol": pos.get("symbol", "UNKNOWN"),
                    "size": pos.get("quantity", pos.get("size", 0)),
                    "entry_price": pos.get("entryPrice", pos.get("entry_price", 0)),
                    "current_price": pos.get("markPrice", pos.get("current_price", 0)),
                    "pnl": pos.get("unrealizedPnl", pos.get("pnl", 0))
                }
                formatted_positions.append(formatted_pos)
                
            return formatted_positions
        else:
            logger.warning("No client available to get positions")
            return []
    except Exception as e:
        logger.error(f"Error getting positions: {e}")
        # Fallback to mock data if there's an error
        return [
            {
                "id": "pos_1",
                "symbol": "BTC/USD",
                "size": 0.5,
                "entry_price": 45000,
                "current_price": 47500,
                "pnl": 1250
            },
            {
                "id": "pos_2", 
                "symbol": "ETH/USD",
                "size": 2.0,
                "entry_price": 3000,
                "current_price": 2900,
                "pnl": -200
            }
        ]

@app.get("/trades")
async def get_trades(limit: int = 10):
    """Get the list of recent trades."""
    # TODO: Return actual recent trades
    return [
        {
            "id": "trade_1",
            "symbol": "BTC/USD",
            "side": "BUY",
            "size": 0.5,
            "price": 45000,
            "timestamp": get_timestamp()
        },
        {
            "id": "trade_2",
            "symbol": "ETH/USD", 
            "side": "SELL",
            "size": 1.0,
            "price": 3200,
            "timestamp": get_timestamp()
        }
    ][:limit]

@app.post("/open_trade")
async def open_trade(trade: dict):
    """Open a new trade."""
    # TODO: Validate trade parameters
    # TODO: Open actual trade
    logger.info(f"Opening trade: {trade}")
    return {"status": "success", "trade_id": f"trade_{get_timestamp()}"}

@app.post("/close_trade")
async def close_trade(trade_id: str):
    """Close an open trade."""
    # TODO: Validate trade ID
    # TODO: Close actual trade
    logger.info(f"Closing trade: {trade_id}")
    return {"status": "success"}

async def start_api_server():
    """Start the FastAPI server for the agent API."""
    from fastapi import FastAPI, HTTPException, Query, Request, Response
    from fastapi.middleware.cors import CORSMiddleware
    import uvicorn
    
    app = FastAPI(title="PerplexityTrader Agent API")
    
    # Add CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    
    @app.get("/health")
    async def health_check():
        """Health check endpoint."""
        return {"status": "ok"}
    
    @app.get("/status")
    async def get_status():
        """Get the current status of the agent."""
        global client
        
        try:
            # Get account info
            account_info = None
            if client:
                try:
                    if hasattr(client, "get_account_info"):
                        account_info = await client.get_account_info()
                    elif hasattr(client, "get_user_account_data"):
                        account_info = await client.get_user_account_data()
                except Exception as e:
                    logger.error(f"Error getting account info: {e}")
            
            # Get active positions
            positions = []
            if client:
                try:
                    if hasattr(client, "get_positions"):
                        positions = await client.get_positions()
                    elif hasattr(client, "get_user_positions"):
                        positions = await client.get_user_positions()
                except Exception as e:
                    logger.error(f"Error getting positions: {e}")
            
            return {
                "status": "running",
                "mock_trading": MOCK_TRADING,
                "account_info": account_info,
                "positions": positions,
                "uptime": int(time.time() - start_time)
            }
        except Exception as e:
            logger.error(f"Error in status endpoint: {e}")
            return {"status": "error", "message": str(e)}
    
    @app.get("/positions")
    async def get_positions():
        """Get current positions."""
        global client
        
        try:
            positions = []
            if client:
                try:
                    if hasattr(client, "get_positions"):
                        positions = await client.get_positions()
                    elif hasattr(client, "get_user_positions"):
                        positions = await client.get_user_positions()
                except Exception as e:
                    logger.warning(f"Client does not have get_positions or get_account_details method")
            
            return positions
        except Exception as e:
            logger.error(f"Error in positions endpoint: {e}")
            return []
    
    @app.post("/add_mock_position")
    async def add_mock_position(position: dict):
        """Add a mock position for testing purposes."""
        global client
        
        try:
            if client and hasattr(client, "positions"):
                # Check if this is a MockBluefinClient
                if client.__class__.__name__ == "MockBluefinClient":
                    # Add the position to the mock client's positions list
                    client.positions.append(position)
                    logger.info(f"Added mock position: {position}")
                    return {"status": "success", "message": "Mock position added"}
                else:
                    logger.warning("Cannot add mock position to non-mock client")
                    return {"status": "error", "message": "Not a mock client"}
            else:
                logger.warning("Client does not support mock positions")
                return {"status": "error", "message": "Client does not support mock positions"}
        except Exception as e:
            logger.error(f"Error adding mock position: {e}")
            return {"status": "error", "message": str(e)}
    
    @app.get("/trades")
    async def get_trades():
        """Get trade history."""
        # This is a mock implementation for now
        return [
            {
                "id": "trade_1",
                "symbol": "BTC/USD",
                "side": "BUY",
                "size": 0.5,
                "price": 45000,
                "timestamp": datetime.now().strftime("%Y%m%d_%H%M%S")
            },
            {
                "id": "trade_2",
                "symbol": "ETH/USD",
                "side": "SELL",
                "size": 1.0,
                "price": 3200,
                "timestamp": datetime.now().strftime("%Y%m%d_%H%M%S")
            }
        ]
    
    @app.post("/open_trade")
    async def open_trade(alert: dict):
        """Open a new trade based on the provided alert."""
        logger.info(f"Opening trade: {alert}")
        
        try:
            # Process the alert
            await process_alert(alert)
            return {"status": "success", "trade_id": f"trade_{datetime.now().strftime('%Y%m%d_%H%M%S')}"}
        except Exception as e:
            logger.error(f"Error opening trade: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @app.post("/close_trade")
    async def close_trade(trade_id: str = Query(...)):
        """Close a trade by ID."""
        logger.info(f"Closing trade: {trade_id}")
        
        try:
            global client
            
            if not client:
                raise HTTPException(status_code=422, detail="Trading client not initialized")
            
            # Get positions
            positions = []
            try:
                if hasattr(client, "get_positions"):
                    positions = await client.get_positions()
                elif hasattr(client, "get_user_positions"):
                    positions = await client.get_user_positions()
            except Exception as e:
                logger.error(f"Error getting positions: {e}")
                positions = []
            
            # Find the position by ID
            position = next((p for p in positions if p.get("id") == trade_id), None)
            
            if not position:
                raise HTTPException(status_code=422, detail=f"Position {trade_id} not found")
            
            # Close the position
            if hasattr(client, "close_position"):
                result = await client.close_position(position.get("symbol"))
                return {"status": "success", "result": result}
            else:
                raise HTTPException(status_code=422, detail="Client does not support closing positions")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error closing trade: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    @app.post("/analyze")
    async def analyze_market(request: dict):
        """Analyze the market using Claude."""
        try:
            # Extract data from request
            symbol = request.get("symbol", "BTC/USD")
            timeframe = request.get("timeframe", "1h")
            data = request.get("data", {})
            
            # Call Claude for analysis
            analysis = await analyze_with_claude(symbol, timeframe, data)
            
            return analysis
        except Exception as e:
            logger.error(f"Error in analyze endpoint: {e}")
            raise HTTPException(status_code=500, detail=str(e))
    
    # Start the server
    port = int(os.getenv("PORT", "5002"))
    config = uvicorn.Config(app, host="0.0.0.0", port=port)
    server = uvicorn.Server(config)
    logger.info(f"API server started on port {port}")
    
    # Also start a Flask server for compatibility with existing code
    import threading
    def run_flask():
        from flask import Flask, request, jsonify
        flask_app = Flask(__name__)
        
        @flask_app.route('/health', methods=['GET'])
        def flask_health():
            return jsonify({"status": "ok"})
        
        @flask_app.route('/webhook', methods=['POST'])
        def webhook():
            try:
                data = request.json
                logger.info(f"Received webhook: {data}")
                
                # Save the alert to the alerts directory
                os.makedirs("alerts", exist_ok=True)
                alert_file = os.path.join("alerts", f"alert_{int(time.time())}.json")
                with open(alert_file, "w") as f:
                    json.dump(data, f)
                
                return jsonify({"status": "success"})
            except Exception as e:
                logger.error(f"Error processing webhook: {e}")
                return jsonify({"status": "error", "message": str(e)}), 500
        
        flask_port = int(os.getenv("FLASK_RUN_PORT", "5000"))
        flask_app.run(host="0.0.0.0", port=flask_port)
    
    threading.Thread(target=run_flask, daemon=True).start()
    
    await server.serve()

async def calculate_position_size(symbol, side, risk_percentage=None, stop_loss_percentage=None):
    """
    Calculate the appropriate position size based on account balance and risk parameters.
    
    Args:
        symbol (str): The trading symbol (e.g., 'BTC-PERP')
        side (str): The order side ('BUY' or 'SELL')
        risk_percentage (float, optional): The percentage of account to risk (0.01 = 1%)
        stop_loss_percentage (float, optional): The percentage for stop loss (0.05 = 5%)
        
    Returns:
        float: The calculated position size
    """
    global client
    
    # Use default risk parameters if not provided
    risk_percentage = risk_percentage or RISK_PARAMS.get("max_risk_per_trade", 0.02)
    stop_loss_percentage = stop_loss_percentage or RISK_PARAMS.get("stop_loss_percentage", 0.05)
    
    try:
        # Get margin bank balance
        # Based on https://bluefin-exchange.readme.io/reference/get-deposit-withdraw-usdc-from-marginbank
        if hasattr(client, 'get_margin_bank_balance'):
            margin_balance = await client.get_margin_bank_balance()
            logger.info(f"Margin bank balance: {margin_balance} USDC")
        else:
            # Fallback to account details
            account_details = await client.get_account_details()
            margin_balance = account_details.get("margin_balance", 0)
            logger.info(f"Account margin balance: {margin_balance} USDC")
        
        # Calculate the dollar amount to risk
        risk_amount = margin_balance * risk_percentage
        logger.info(f"Risking {risk_percentage*100}% of balance: {risk_amount} USDC")
        
        # Get current market price for the symbol
        current_price = await get_market_price(symbol)
        
        # Calculate position size based on risk and stop loss
        # Formula: Position Size = Risk Amount / (Current Price * Stop Loss Percentage)
        position_size = risk_amount / (current_price * stop_loss_percentage)
        
        # Apply max position size limit
        max_position_size = TRADING_PARAMS.get("max_position_size_usd", 1000) / current_price
        position_size = min(position_size, max_position_size)
        
        # Round to appropriate precision (e.g., 0.001 BTC)
        position_size = round(position_size, 3)
        
        logger.info(f"Calculated position size: {position_size} for {symbol} {side}")
        return position_size
    
    except Exception as e:
        logger.error(f"Error calculating position size: {e}")
        logger.error(traceback.format_exc())
        # Return a safe default
        return 0.001  # Minimal position size as fallback

async def get_market_price(symbol):
    """
    Get the current market price for a symbol from Bluefin Exchange.
    
    This function first tries to use the BluefinMarket utility, then falls back
    to direct API calls, and finally to default values if all else fails.
    
    Args:
        symbol (str): The trading symbol (e.g., 'BTC-PERP')
        
    Returns:
        float: The current market price
    """
    global client
    
    try:
        # First try using BluefinMarket utility if available
        try:
            from core.bluefin_market import get_price
            price = await get_price(symbol)
            if price is not None:
                logger.info(f"Got market price from BluefinMarket utility for {symbol}: {price}")
                return price
        except ImportError:
            # BluefinMarket utility not available, falling back to direct methods
            logger.debug("BluefinMarket utility not available, using fallback methods")
        
        # Normalize symbol format (Bluefin API may require a specific format)
        if "-PERP" not in symbol and "/" in symbol:
            api_symbol = symbol.replace("/", "-") + "-PERP"
        else:
            api_symbol = symbol
            
        logger.info(f"Getting market price for {api_symbol}")
        
        # Try to get market price directly from Bluefin API
        if client and hasattr(client, '_request'):
            try:
                # Try to get exchange info for the symbol
                response = await client._request("GET", f"/marketData?symbol={api_symbol}")
                if response and isinstance(response, dict) and "marketPrice" in response:
                    price = float(response["marketPrice"]) / 1e18
                    logger.info(f"Got oracle price from Bluefin API for {api_symbol}: {price}")
                    return price
            except Exception as e:
                logger.warning(f"Error getting price from Bluefin API: {e}")
        
        # Try to get market price using client's method
        if client and hasattr(client, 'get_market_price'):
            try:
                price = await client.get_market_price(api_symbol)
                logger.info(f"Got market price using client for {api_symbol}: {price}")
                return float(price)
            except Exception as e:
                logger.warning(f"Error getting price using client's get_market_price: {e}")
        
        # Try to get orderbook and use mid price
        if client and hasattr(client, 'get_orderbook'):
            try:
                orderbook = await client.get_orderbook(api_symbol)
                if orderbook and 'bids' in orderbook and 'asks' in orderbook:
                    if orderbook['bids'] and orderbook['asks']:
                        bid = float(orderbook['bids'][0][0])
                        ask = float(orderbook['asks'][0][0])
                        mid_price = (bid + ask) / 2
                        logger.info(f"Calculated mid price for {api_symbol}: {mid_price}")
                        return mid_price
            except Exception as e:
                logger.warning(f"Error getting orderbook: {e}")
        
        # Fallback to default prices for common symbols
        default_prices = {
            'BTC-PERP': 50000,
            'ETH-PERP': 3000,
            'SUI-PERP': 1.5,
            'SOL-PERP': 100,
            'BNB-PERP': 400
        }
        
        # Extract base symbol from the full symbol
        base_symbol = symbol.split('-')[0] if '-' in symbol else symbol.split('/')[0] if '/' in symbol else symbol
        
        # Try to find a matching default price
        for key, price in default_prices.items():
            if base_symbol in key:
                logger.warning(f"Using default price for {symbol}: {price}")
                return price
        
        # Last resort fallback
        logger.warning(f"No price found for {symbol}, using default price: 100")
        return 100
    
    except Exception as e:
        logger.error(f"Error getting market price: {e}")
        logger.error(traceback.format_exc())
        # Return a safe default
        return 100

async def ensure_leverage(symbol, target_leverage):
    """
    Ensure that the leverage for a symbol is set to the target value.
    
    Args:
        symbol (str): The trading symbol (e.g., 'BTC-PERP')
        target_leverage (int): The desired leverage value
        
    Returns:
        bool: True if leverage is set successfully, False otherwise
    """
    global client
    
    try:
        # Get current leverage
        current_leverage = await client.get_user_leverage(symbol)
        logger.info(f"Current leverage for {symbol}: {current_leverage}x")
        
        # Check if adjustment is needed
        if current_leverage == target_leverage:
            logger.info(f"Leverage for {symbol} already set to {target_leverage}x")
            return True
            
        # Adjust leverage
        logger.info(f"Adjusting leverage for {symbol} from {current_leverage}x to {target_leverage}x")
        
        # Try different method names that might be available in the client
        if hasattr(client, 'set_leverage'):
            result = await client.set_leverage(symbol, target_leverage)
        elif hasattr(client, 'adjust_leverage'):
            result = await client.adjust_leverage(symbol, target_leverage)
        else:
            logger.warning(f"No method available to set leverage for {symbol}")
            return False
        
        if isinstance(result, dict) and result.get('success', False):
            logger.info(f"Successfully adjusted leverage for {symbol} to {target_leverage}x")
            return True
        else:
            logger.warning(f"Failed to adjust leverage for {symbol}: {result}")
            return False
            
    except Exception as e:
        logger.error(f"Error adjusting leverage for {symbol}: {e}")
        logger.error(traceback.format_exc())
        return False

# Define a simple Order class to track order state
class Order:
    def __init__(self, 
                 symbol: str, 
                 side: str, 
                 quantity: float, 
                 order_type: str,
                 price: float = 0.0,
                 leverage: int = 1,
                 order_hash: str = "",
                 status: str = "pending"):
        self.symbol = symbol
        self.side = side
        self.quantity = quantity
        self.order_type = order_type
        self.price = price
        self.leverage = leverage
        self.hash = order_hash
        self.status = status
        self.created_at = datetime.now()
        
        # Settlement status fields
        self.settlement_status = "pending"
        self.requeue_count = 0
        self.cancelled = False
        self.fill_price = 0.0
        self.matched_quantity = 0.0
        self.is_maker = False
        
    def __str__(self):
        return f"Order({self.symbol}, {self.side}, {self.quantity}, {self.order_type}, price={self.price}, leverage={self.leverage}, status={self.status}, settlement_status={self.settlement_status}, requeue_count={self.requeue_count}, cancelled={self.cancelled})"
    
    def __repr__(self):
        return self.__str__()

if __name__ == "__main__":
    asyncio.run(main())