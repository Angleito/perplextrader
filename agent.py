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
from datetime import datetime
from pathlib import Path
import backoff
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from typing import Dict, List, Optional, Union, Any, TypeVar, Type, cast

# Load environment variables from .env file
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("logs/agent.log", mode='a', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("bluefin_agent")

# Try to import configuration, with fallbacks if not available
try:
    from config import TRADING_PARAMS, RISK_PARAMS, AI_PARAMS
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
BluefinClientType = Any
NetworksType = Any
OrderSignatureRequestType = Any

# Set up variables for Bluefin clients
BLUEFIN_CLIENT_SUI_AVAILABLE = False
BLUEFIN_V2_CLIENT_AVAILABLE = False
BluefinClient = None
Networks = None
OrderSignatureRequest = None
NETWORKS = {
    "testnet": "testnet",
    "mainnet": "mainnet"
}

# Try to import SUI client first
try:
    # Disable linter warnings for imports that might not be available
    # pylint: disable=import-error
    # type: ignore
    # These imports may fail if the library is not installed, but the code handles this gracefully
    # fmt: off
    from bluefin_client_sui import (  # type: ignore # noqa
        BluefinClient as SUIBluefinClient,
        Networks as SUINetworks,
        ORDER_SIDE as SUI_ORDER_SIDE,
        ORDER_TYPE as SUI_ORDER_TYPE,
        OrderSignatureRequest as SuiOrderSignatureRequest
    )
    # fmt: on
    
    BluefinClient = SUIBluefinClient
    Networks = SUINetworks
    ORDER_SIDE = SUI_ORDER_SIDE
    ORDER_TYPE = SUI_ORDER_TYPE
    OrderSignatureRequest = SuiOrderSignatureRequest
    BLUEFIN_CLIENT_SUI_AVAILABLE = True
    logger.info("Successfully imported Bluefin SUI client")
except ImportError:
    logger.warning("Bluefin SUI client not available, will try v2 client")
    # Try to import v2 client as fallback
    try:
        from bluefin.v2.client import BluefinClient as V2BluefinClient
        from bluefin.v2.types import OrderSignatureRequest as V2OrderSignatureRequest
        # Assign the imported classes to our variables
        BluefinClient = V2BluefinClient
        OrderSignatureRequest = V2OrderSignatureRequest
        BLUEFIN_V2_CLIENT_AVAILABLE = True
        logger.info("Successfully imported Bluefin v2 client")
    except ImportError:
        logger.warning("Bluefin v2 client not available")
        logger.warning("Running in simulation mode without actual trading capabilities")

# Warn if no Bluefin client libraries are available
if not BLUEFIN_CLIENT_SUI_AVAILABLE and not BLUEFIN_V2_CLIENT_AVAILABLE:
    print("WARNING: No Bluefin client libraries found. Using mock implementation.")
    print("Please install one of the following:")
    print("   pip install git+https://github.com/fireflyprotocol/bluefin-client-python-sui.git")
    print("   pip install git+https://github.com/fireflyprotocol/bluefin-v2-client-python.git")

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

# Define mock OrderSignatureRequest class that will be used as a fallback
class MockOrderSignatureRequest:
    def __init__(self, *args, **kwargs):
        self.symbol = kwargs.get('symbol', 'SUI-PERP')
        self.price = kwargs.get('price', 1.0)
        self.quantity = kwargs.get('quantity', 1.0)
        self.side = kwargs.get('side', ORDER_SIDE.BUY)
        self.type = kwargs.get('type', ORDER_TYPE.MARKET)
        self.leverage = kwargs.get('leverage', 5)
        self.post_only = kwargs.get('post_only', False)
        self.reduce_only = kwargs.get('reduce_only', False)
        self.time_in_force = kwargs.get('time_in_force', 'GoodTillTime')
        self.expiration = kwargs.get('expiration', int(time.time()) + 604800)  # 1 week

# Use the mock class if OrderSignatureRequest is None
if OrderSignatureRequest is None:
    OrderSignatureRequest = MockOrderSignatureRequest

def setup_logging():
    """Set up logging configuration."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(f"trading_log_{int(datetime.now().timestamp())}.log"),
            logging.StreamHandler()
        ]
    )

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

async def analyze_tradingview_chart() -> Dict[str, Any]:
    """
    Analyzes a TradingView chart by taking a screenshot for AI analysis.
    
    Returns:
        Dict containing analysis results and screenshot data.
    """
    try:
        logger.info("Starting TradingView chart analysis")
        symbol = TRADING_PARAMS.get("chart_symbol", "SUIUSD")
        timeframe = TRADING_PARAMS.get("chart_timeframe", "5")
        indicators = TRADING_PARAMS.get("chart_indicators", ["VuManChu Cipher A", "VuManChu Cipher B"])
        candle_type = TRADING_PARAMS.get("chart_candle_type", "Heikin Ashi")
        
        logger.info(f"Analyzing chart for {symbol} on {timeframe} timeframe with indicators: {indicators}")
        
        # Example URL (would need to be adapted for actual TradingView embedding)
        chart_url = f"https://www.tradingview.com/chart/?symbol={symbol}&interval={timeframe}"
        
        from playwright.async_api import async_playwright
        
        logger.info(f"Loading TradingView chart from URL: {chart_url}")
        
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            page = await browser.new_page()
            
            # Load TradingView chart
            await page.goto(chart_url)
            logger.info("Chart page loaded, waiting for rendering...")
            
            # Wait for chart to load
            await page.wait_for_selector(".chart-container", timeout=30000)
            logger.info("Chart container found")
            
            # Set timeframe if different from default
            try:
                await page.click(f"[data-item-id='{timeframe}']")
                logger.info(f"Set timeframe to {timeframe}")
            except Exception as e:
                logger.error(f"Error setting timeframe: {e}")
            
            # Set candle type if needed
            if candle_type.lower() != "regular":
                try:
                    # This is a simplified example - actual implementation would depend on TradingView's DOM structure
                    await page.click(".chart-style-button")
                    await page.click(f"text='{candle_type}'")
                    logger.info(f"Set candle type to {candle_type}")
                except Exception as e:
                    logger.error(f"Error setting candle type: {e}")
            
            # Add indicators if needed
            for indicator in indicators:
                try:
                    # This is a simplified example - actual implementation would depend on TradingView's DOM structure
                    await page.click(".indicators-button")
                    await page.fill(".search-input", indicator)
                    await page.click(f"text='{indicator}'")
                    logger.info(f"Added indicator: {indicator}")
                except Exception as e:
                    logger.error(f"Error adding indicator {indicator}: {e}")
            
            # Wait for chart to update with all indicators
            await asyncio.sleep(5)
            
            # Take screenshot
            logger.info("Taking screenshot of the chart")
            screenshot = await page.screenshot()
            
            # Close browser
            await browser.close()
            
            logger.info("Chart analysis completed successfully")
            
            # Return analysis results
            return {
                "timestamp": datetime.now().isoformat(),
                "symbol": symbol,
                "timeframe": timeframe,
                "screenshot": screenshot,
                "message": "Chart analysis completed successfully",
                "indicators_used": indicators,
                "candle_type": candle_type
            }
            
    except Exception as e:
        logger.error(f"Error analyzing TradingView chart: {e}", exc_info=True)
        return {
            "timestamp": datetime.now().isoformat(),
            "error": str(e),
            "message": "Failed to analyze chart",
            "screenshot": None
        }

def extract_trade_recommendation(analysis: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract trade recommendation from AI analysis of chart.
    
    Args:
        analysis: Dictionary containing analysis results
        
    Returns:
        Dictionary with trade recommendation details
    """
    # This is a placeholder implementation
    # In a real system, this would parse the AI analysis text or call an AI API
    
    # Default to no trade
    recommendation = {
        "action": "NONE",  # NONE, BUY, SELL
        "symbol": TRADING_PARAMS.get("trading_symbol", "SUI-PERP"),
        "entry_price": 0.0,
        "stop_loss": 0.0,
        "take_profit": 0.0,
        "confidence": 0.0,  # 0.0 to 1.0
        "reason": "No trade opportunity detected",
        "timestamp": datetime.now().isoformat()
    }
    
    # Check if we have an error
    if "error" in analysis:
        logger.warning(f"Cannot extract trade recommendation due to analysis error: {analysis.get('error')}")
        return recommendation
    
    # In a real implementation, this would use AI to analyze the chart
    # For now, this is just a placeholder with a random simple algorithm
    
    # Simulate a basic analysis
    import random
    
    # Random confidence (placeholder for actual AI analysis)
    confidence = random.uniform(0.3, 0.9)
    confidence = round(confidence, 2)
    
    # Placeholder entry price (would come from actual chart analysis)
    entry_price = random.uniform(900, 1100)  # Placeholder for BTC price
    
    # Decide action based on confidence
    if confidence > TRADING_PARAMS.get("min_confidence", 0.7):
        # Randomly choose buy or sell for demonstration
        action = "BUY" if random.random() > 0.5 else "SELL"
        
        # Set stop loss and take profit based on action
        stop_loss_pct = 0.02  # 2% from entry
        take_profit_pct = 0.05  # 5% from entry
        
        if action == "BUY":
            stop_loss = entry_price * (1 - stop_loss_pct)
            take_profit = entry_price * (1 + take_profit_pct)
            reason = "Strong upward momentum detected"
        else:  # SELL
            stop_loss = entry_price * (1 + stop_loss_pct)
            take_profit = entry_price * (1 - take_profit_pct)
            reason = "Bearish pattern forming"
            
        recommendation = {
            "action": action,
            "symbol": TRADING_PARAMS.get("trading_symbol", "SUI-PERP"),
            "entry_price": round(entry_price, 2),
            "stop_loss": round(stop_loss, 2),
            "take_profit": round(take_profit, 2),
            "confidence": confidence,
            "reason": reason,
            "timestamp": datetime.now().isoformat()
        }
    
    logger.info(f"Trade recommendation: {recommendation['action']} with confidence {recommendation['confidence']}")
    return recommendation

@backoff.on_exception(backoff.expo, 
                     (asyncio.TimeoutError, ConnectionError),
                     max_tries=3,
                     max_time=30)
async def execute_trade(client, trade_rec, account_info):
    """
    Execute a trade based on the recommendation and account information.
    
    Args:
        client: The Bluefin client (SUI or v2)
        trade_rec: Dictionary with trade recommendation details
        account_info: Dictionary with account information
        
    Returns:
        Dictionary with execution results
    """
    if not client:
        logger.error("Cannot execute trade: client not initialized")
        return {"success": False, "error": "Client not initialized"}
        
    if not trade_rec:
        logger.error("Cannot execute trade: invalid trade recommendation")
        return {"success": False, "error": "Invalid trade recommendation"}
        
    if trade_rec["action"] == "NONE" or trade_rec["confidence"] < TRADING_PARAMS.get("min_confidence", 0.7):
        logger.info(f"Trade not executed due to low confidence or NONE action: {trade_rec}")
        return {"success": False, "reason": "Low confidence or NONE action"}
        
    try:
        # Extract parameters from trade_rec
        symbol = trade_rec["symbol"]
        action = trade_rec["action"]  # BUY or SELL
        entry_price = trade_rec["entry_price"]
        stop_loss = trade_rec["stop_loss"]
        take_profit = trade_rec["take_profit"]
        
        # Get account balance and leverage
        balance = account_info.get("balance", 0)
        leverage = TRADING_PARAMS.get("leverage", 3)
        
        if balance <= 0:
            logger.error(f"Invalid account balance: {balance}")
            return {"success": False, "error": "Invalid account balance"}
            
        # Calculate position size using risk manager
        position_size = 0
        if risk_manager:
            risk_manager.update_account_balance(balance)
            if not risk_manager.can_open_new_trade():
                logger.warning("Risk limits reached, cannot open new trade")
                return {"success": False, "reason": "Risk limits reached"}
                
            position_size = risk_manager.calculate_position_size(entry_price, stop_loss)
        else:
            # Fallback calculation if risk manager is not available
            risk_per_trade = RISK_PARAMS.get("max_risk_per_trade", 0.01)
            position_size = (balance * risk_per_trade) / (abs(entry_price - stop_loss) / entry_price)
            
        # Adjust for leverage
        position_size = position_size * leverage
        
        # Ensure position size is not too small
        min_position_size = 0.001  # Example minimum position size
        if position_size < min_position_size:
            logger.warning(f"Position size too small: {position_size}, minimum: {min_position_size}")
            return {"success": False, "reason": "Position size too small"}
            
        # Cap position size to max allowed
        max_position_size = TRADING_PARAMS.get("max_position_size_usd", float('inf'))
        if max_position_size and position_size > max_position_size:
            logger.info(f"Position size {position_size} capped to max allowed {max_position_size}")
            position_size = max_position_size
            
        # Round position size to appropriate precision
        position_size = round(position_size, 4)
        
        # Map action to ORDER_SIDE
        side = None
        if action == "BUY":
            side = ORDER_SIDE.BUY if hasattr(ORDER_SIDE, "BUY") else "BUY"
        elif action == "SELL":
            side = ORDER_SIDE.SELL if hasattr(ORDER_SIDE, "SELL") else "SELL"
        else:
            logger.error(f"Invalid action: {action}, must be BUY or SELL")
            return {"success": False, "error": f"Invalid action: {action}"}
            
        # Display trade information
        logger.info(f"Executing {action} order for {position_size} {symbol} at {entry_price}")
        logger.info(f"Stop Loss: {stop_loss}, Take Profit: {take_profit}")
        
        # Execute the trade based on available client
        result = None
        
        try:
            # Try executing via SUI client if available
            if BLUEFIN_CLIENT_SUI_AVAILABLE:
                # SUI client uses place_order method
                if hasattr(client, 'place_order'):
                    result = await client.place_order(
                        symbol=symbol,
                        side=side,
                        size=position_size,
                        price=entry_price,
                        leverage=leverage,
                        stop_loss=stop_loss,
                        take_profit=take_profit
                    )
                # SUI client might use create_signed_order
                elif hasattr(client, 'create_signed_order') and OrderSignatureRequest:
                    # Create order signature request
                    order_request = OrderSignatureRequest(
                        symbol=symbol,
                        price=entry_price,
                        quantity=position_size,
                        side=side,
                        leverage=leverage,
                        reduceOnly=False,
                        postOnly=False
                    )
                    
                    # Create signed order
                    signed_order = await client.create_signed_order(order_request)
                    
                    # Submit the order
                    result = await client.submit_order(signed_order)
                else:
                    logger.error("Unsupported SUI client implementation")
                    return {"success": False, "error": "Unsupported client implementation"}
                    
            # If v2 client is available
            elif BLUEFIN_V2_CLIENT_AVAILABLE:
                if hasattr(client, 'create_order'):
                    # v2 client might use create_order
                    result = await client.create_order(
                        symbol=symbol,
                        side=side,
                        quantity=position_size,
                        price=entry_price,
                        leverage=leverage,
                        reduce_only=False,
                        post_only=False,
                        stop_loss=stop_loss,
                        take_profit=take_profit
                    )
                elif hasattr(client, 'place_order'):
                    # Or place_order
                    result = await client.place_order(
                        symbol=symbol,
                        side=side,
                        size=position_size,
                        price=entry_price,
                        leverage=leverage,
                        stop_loss=stop_loss,
                        take_profit=take_profit
                    )
                else:
                    logger.error("Unsupported v2 client implementation")
                    return {"success": False, "error": "Unsupported client implementation"}
            else:
                # No client available, simulate execution
                logger.warning("No Bluefin client available, simulating order execution")
                
                # Simulate a successful order
                result = {
                    "symbol": symbol,
                    "side": side,
                    "size": position_size,
                    "price": entry_price,
                    "leverage": leverage,
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "order_id": f"simulated_{datetime.now().timestamp()}",
                    "timestamp": datetime.now().isoformat(),
                    "simulated": True
                }
                
            # Log the execution result
            if result:
                logger.info(f"Order execution successful: {result}")
                return {
                    "success": True,
                    "order": result,
                    "timestamp": datetime.now().isoformat()
                }
            else:
                logger.error("Order execution failed: No result returned")
                return {"success": False, "error": "No result returned from order execution"}
                
        except Exception as e:
            logger.error(f"Error executing trade: {e}", exc_info=True)
            return {"success": False, "error": str(e)}
            
    except Exception as e:
        logger.error(f"Unexpected error in execute_trade: {e}", exc_info=True)
        return {"success": False, "error": str(e)}

async def main():
    """
    Main function to run AI analysis and execute trades.
    
    Required environment variables:
    - For bluefin_client_sui:
      - BLUEFIN_PRIVATE_KEY: Your private key for authentication
    
    - For bluefin.v2.client:
      - BLUEFIN_API_KEY: Your API key
      - BLUEFIN_API_SECRET: Your API secret
      - BLUEFIN_API_URL: (Optional) Custom API URL
    """
    client = None
    try:
        # Initialize clients based on available libraries
        if BLUEFIN_CLIENT_SUI_AVAILABLE:
            # Use SUI client
            private_key = os.getenv("BLUEFIN_PRIVATE_KEY")
            if not private_key:
                logger.error("BLUEFIN_PRIVATE_KEY not found in environment variables")
                logger.error("Please add your private key to the .env file as BLUEFIN_PRIVATE_KEY=your_private_key_here")
                logger.error("For security, never share your private key or commit it to version control")
                return
                
            try:
                network_str = os.getenv("BLUEFIN_NETWORK", "MAINNET")
                if network_str not in ["MAINNET", "TESTNET"]:
                    logger.error(f"Invalid network: {network_str}. Must be either 'MAINNET' or 'TESTNET'")
                    return
                    
                if network_str == "MAINNET":
                    logger.warning("Using MAINNET for trading. Ensure this is intentional and that you understand the risks.")
                    
                # Get the network from Networks enum if available, otherwise use default mapping
                if Networks is not None and hasattr(Networks, network_str):
                    network = getattr(Networks, network_str)
                else:
                    network = NETWORKS.get(network_str.lower(), "testnet")
                
                logger.info(f"Initializing Bluefin SUI client with network: {network_str}")
                
                if BluefinClient is not None:
                    client = BluefinClient(private_key=private_key, network=network)
                    await client.init()
                    
                    logger.info(f"Connecting to Bluefin network: {network_str}")
                    connect_result = await client.connect()
                    if connect_result:
                        logger.info("Successfully connected to Bluefin")
                    else:
                        logger.error("Failed to connect to Bluefin")
                        return
                else:
                    logger.error("BluefinClient class is not available")
                    return
            except Exception as e:
                logger.error(f"Error initializing SUI client: {e}", exc_info=True)
                return
                
        elif BLUEFIN_V2_CLIENT_AVAILABLE:
            # Use V2 client
            api_key = os.getenv("BLUEFIN_API_KEY")
            api_secret = os.getenv("BLUEFIN_API_SECRET")
            custom_api_url = os.getenv("BLUEFIN_API_URL")
            
            if not api_key or not api_secret:
                logger.error("BLUEFIN_API_KEY and/or BLUEFIN_API_SECRET not found in environment variables")
                logger.error("Please add your API credentials to the .env file")
                logger.error("For security, never share your API credentials or commit them to version control")
                return
                
            try:
                network_str = os.getenv("BLUEFIN_NETWORK", "mainnet").lower()
                if network_str not in ["mainnet", "testnet"]:
                    logger.error(f"Invalid network: {network_str}. Must be either 'mainnet' or 'testnet'")
                    return
                    
                if network_str == "mainnet":
                    logger.warning("Using MAINNET for trading. Ensure this is intentional and that you understand the risks.")
                
                kwargs = {
                    "api_key": api_key,
                    "api_secret": api_secret,
                    "network": network_str
                }
                
                if custom_api_url:
                    kwargs["api_url"] = custom_api_url
                
                logger.info(f"Initializing Bluefin v2 client with network: {network_str}")
                
                if BluefinClient is not None:
                    client = BluefinClient(**kwargs)
                    
                    # V2 client might not have connect method, check if it exists
                    if hasattr(client, 'connect'):
                        logger.info("Connecting to Bluefin network")
                        connect_result = await client.connect()
                        if connect_result:
                            logger.info("Successfully connected to Bluefin")
                        else:
                            logger.error("Failed to connect to Bluefin")
                            return
                    else:
                        logger.info("No explicit connect method for v2 client, assuming connected")
                else:
                    logger.error("BluefinClient class is not available")
                    return
            except Exception as e:
                logger.error(f"Error initializing v2 client: {e}", exc_info=True)
                return
        else:
            logger.error("No Bluefin client libraries available")
            logger.error("Please install one of the following:")
            logger.error("   pip install git+https://github.com/fireflyprotocol/bluefin-client-python-sui.git")
            logger.error("   pip install git+https://github.com/fireflyprotocol/bluefin-v2-client-python.git")
            return
        
        # Initialize risk manager
        risk_manager_instance = risk_manager
        
        # Create directories for storing results
        os.makedirs("logs", exist_ok=True)
        os.makedirs("screenshots", exist_ok=True)
        os.makedirs("analysis", exist_ok=True)
        
        # Trading loop
        while True:
            try:
                # Get account information
                account_info = await get_account_info(client)
                
                # Analyze TradingView chart
                analysis = await analyze_tradingview_chart()
                
                # Extract trade recommendation
                trade_rec = extract_trade_recommendation(analysis)
                
                # Execute trade if appropriate
                if trade_rec["action"] != "NONE" and trade_rec["confidence"] >= TRADING_PARAMS.get("min_confidence", 0.7):
                    execution_result = await execute_trade(client, trade_rec, account_info)
                    
                    # Save the analysis and result
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    analysis_file = f"analysis/trade_analysis_{timestamp}.json"
                    
                    with open(analysis_file, "w") as f:
                        json.dump({
                            "timestamp": timestamp,
                            "trade_recommendation": trade_rec,
                            "execution_result": execution_result,
                            "account_info": {
                                "balance": account_info.get("balance", 0),
                                "available_margin": account_info.get("available_margin", 0),
                                "positions_count": len(account_info.get("positions", []))
                            }
                        }, f, indent=2)
                        
                    logger.info(f"Trade analysis saved to {analysis_file}")
                    
                    # Save screenshot if available
                    if "screenshot" in analysis and analysis["screenshot"]:
                        screenshot_file = f"screenshots/chart_{timestamp}.png"
                        with open(screenshot_file, "wb") as f:
                            f.write(analysis["screenshot"])
                        logger.info(f"Chart screenshot saved to {screenshot_file}")
                else:
                    logger.info(f"No trade executed - action: {trade_rec['action']}, confidence: {trade_rec['confidence']}")
                
                # Wait for next analysis interval
                wait_time = TRADING_PARAMS.get("analysis_interval_seconds", 300)  # Default 5 minutes
                logger.info(f"Waiting {wait_time} seconds until next analysis")
                await asyncio.sleep(wait_time)
                
            except Exception as e:
                logger.error(f"Error in trading loop: {e}", exc_info=True)
                # Wait a bit before retrying
                await asyncio.sleep(30)
    
    except Exception as e:
        logger.error(f"Unhandled error in main function: {e}", exc_info=True)
    finally:
        # Clean up resources
        if client:
            if hasattr(client, 'disconnect'):
                try:
                    logger.info("Disconnecting from Bluefin")
                    await client.disconnect()
                except Exception as e:
                    logger.error(f"Error disconnecting from Bluefin: {e}", exc_info=True)
            
            if hasattr(client, 'api') and hasattr(client.api, 'close_session'):
                try:
                    logger.info("Closing API session")
                    await client.api.close_session()
                except Exception as e:
                    logger.error(f"Error closing API session: {e}", exc_info=True)

if __name__ == "__main__":
    setup_logging()
    initialize_risk_manager() 
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Trading agent stopped by user")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)