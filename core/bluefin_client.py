"""
Bluefin Client Implementation

This module provides a unified interface for interacting with the Bluefin Exchange
API, supporting both private key and API key authentication methods.

Usage:
    from core.bluefin_client import create_bluefin_client
    
    # Create a client instance
    client = await create_bluefin_client()
    
    # Get account information
    account_info = await client.get_account_info()
    
    # Place an order
    order = await client.place_order(
        symbol="SUI-PERP",
        side="BUY",
        quantity=10,
        price=None,  # Market order
        leverage=10
    )
"""

import os
import logging
import asyncio
import time
from typing import Dict, List, Optional, Union, Any
from bluefin_client_sui import BluefinClient as SuiClient, Networks, SOCKET_EVENTS
from bluefin.v2.client import BluefinClient as ApiClient
from core.agent import Order

logger = logging.getLogger(__name__)

# Define constants for order sides and types
class ORDER_SIDE:
    BUY = "BUY"
    SELL = "SELL"

class ORDER_TYPE:
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"
    TAKE_PROFIT = "TAKE_PROFIT"

# Define missing socket event constants if not available in the library
if not hasattr(SOCKET_EVENTS, 'ORDER_SETTLEMENT_UPDATE'):
    SOCKET_EVENTS.ORDER_SETTLEMENT_UPDATE = type('obj', (object,), {
        'value': 'orderSettlementUpdate'
    })

if not hasattr(SOCKET_EVENTS, 'ORDER_REQUEUE_UPDATE'):
    SOCKET_EVENTS.ORDER_REQUEUE_UPDATE = type('obj', (object,), {
        'value': 'orderRequeueUpdate'
    })

if not hasattr(SOCKET_EVENTS, 'ORDER_CANCELLED_ON_REVERSION_UPDATE'):
    SOCKET_EVENTS.ORDER_CANCELLED_ON_REVERSION_UPDATE = type('obj', (object,), {
        'value': 'orderCancelledOnReversionUpdate'
    })

# Constants
REQUEUE_ADJUSTMENT_THRESHOLD = 2  # Adjust price after this many requeues

class BluefinClientInterface:
    """Interface for Bluefin clients to implement."""
    
    async def get_account_info(self) -> Dict[str, Any]:
        """Get account information including balance and positions."""
        raise NotImplementedError("Method not implemented")
    
    async def get_positions(self) -> List[Dict[str, Any]]:
        """Get current positions."""
        raise NotImplementedError("Method not implemented")
    
    async def place_order(self, 
                          symbol: str, 
                          side: str, 
                          quantity: float, 
                          price: Optional[float] = None,
                          order_type: str = ORDER_TYPE.MARKET,
                          reduce_only: bool = False,
                          time_in_force: str = "GTC",
                          leverage: Optional[int] = None) -> Dict[str, Any]:
        """Place an order on Bluefin Exchange."""
        raise NotImplementedError("Method not implemented")
    
    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancel an order by ID."""
        raise NotImplementedError("Method not implemented")
    
    async def close_position(self, 
                            symbol: str, 
                            quantity: Optional[float] = None) -> Dict[str, Any]:
        """Close a position for the given symbol, optionally specifying quantity."""
        raise NotImplementedError("Method not implemented")
    
    async def set_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        """Set leverage for a symbol."""
        raise NotImplementedError("Method not implemented")
    
    async def get_market_price(self, symbol: str) -> float:
        """Get the current market price for a symbol."""
        raise NotImplementedError("Method not implemented")
    
    async def close(self) -> None:
        """Close the client connection."""
        raise NotImplementedError("Method not implemented")
        
    async def get_user_trades_history(self,
                                     symbol: Optional[str] = None,
                                     maker: Optional[bool] = None,
                                     order_type: Optional[str] = None,
                                     from_id: Optional[str] = None,
                                     start_time: Optional[int] = None,
                                     end_time: Optional[int] = None,
                                     limit: int = 50,
                                     cursor: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Get the user's completed trades history.
        
        Args:
            symbol: Market symbol for which to get trades
            maker: If True, fetch trades where the user is the maker
            order_type: Order type (Market or Limit)
            from_id: Get trades after the provided ID
            start_time: The time after which trades will be fetched from
            end_time: The time before which all trades will be returned
            limit: Total number of records to get (max 50)
            cursor: The particular page number to be fetched
            
        Returns:
            List of trade records
        """
        raise NotImplementedError("Method not implemented")

class BluefinSuiClient(BluefinClientInterface):
    """Client implementation for Bluefin Exchange using SUI private key authentication."""
    
    def __init__(self, private_key: str, network: str = "MAINNET"):
        """Initialize the Bluefin SUI client."""
        self.private_key = private_key
        self.network = getattr(Networks, network)
        self.client = SuiClient(
            True,  # agree to terms and conditions
            self.network,
            self.private_key
        )
        self.initialized = False
        self.orders = {}
        self.requeue_counts = {}
        
        # Set up logging
        self.logger = logging.getLogger(__name__)
    
    async def init(self, onboard_user: bool = False) -> None:
        """
        Initialize the client connection.
        
        Args:
            onboard_user: If True, onboards the user on Bluefin. Must be set to True for first time use.
        """
        if self.initialized:
            return
        
        try:
            await self.client.init(onboard_user)
            
            # Register event handlers
            self.client.on(SOCKET_EVENTS.ORDER_UPDATE.value, self.order_update_handler)
            self.client.on(SOCKET_EVENTS.USER_TRADE.value, self.user_trade_handler)
            
            # Register additional event handlers if available
            if hasattr(SOCKET_EVENTS, 'ORDER_SETTLEMENT_UPDATE'):
                self.client.on(SOCKET_EVENTS.ORDER_SETTLEMENT_UPDATE.value, 
                              self.order_settlement_update_handler)
            
            if hasattr(SOCKET_EVENTS, 'ORDER_REQUEUE_UPDATE'):
                self.client.on(SOCKET_EVENTS.ORDER_REQUEUE_UPDATE.value, 
                              self.order_requeue_update_handler)
            
            if hasattr(SOCKET_EVENTS, 'ORDER_CANCELLED_ON_REVERSION_UPDATE'):
                self.client.on(SOCKET_EVENTS.ORDER_CANCELLED_ON_REVERSION_UPDATE.value, 
                              self.order_cancelled_on_reversion_handler)
            
            self.initialized = True
            self.logger.info("Bluefin SUI client initialized")
        except Exception as e:
            self.logger.error(f"Failed to initialize Bluefin SUI client: {e}")
            raise
    
    async def get_account_info(self) -> Dict[str, Any]:
        """Get account information including balance and positions."""
        account_data = await self.client.get_user_account_data()
        margin_data = await self.client.get_user_margin()
        positions = await self.client.get_user_positions() or []
        
        return {
            "balance": float(account_data.get("totalCollateralValue", 0)),
            "availableMargin": float(margin_data.get("availableMargin", 0)),
            "positions": positions
        }
    
    async def get_positions(self) -> List[Dict[str, Any]]:
        """Get current positions."""
        return await self.client.get_user_positions() or []
    
    async def place_order(self, 
                         symbol: str, 
                         side: str, 
                         quantity: float, 
                         price: Optional[float] = None,
                         order_type: str = ORDER_TYPE.MARKET,
                         reduce_only: bool = False,
                         time_in_force: str = "GTC",
                         leverage: Optional[int] = None) -> Dict[str, Any]:
        """Place an order on Bluefin Exchange."""
        # Set leverage if provided
        if leverage is not None:
            await self.set_leverage(symbol, leverage)
        
        # Convert order parameters
        from bluefin_client_sui import ORDER_SIDE as SUI_ORDER_SIDE
        from bluefin_client_sui import ORDER_TYPE as SUI_ORDER_TYPE
        
        # Map order side
        if side.upper() == ORDER_SIDE.BUY:
            sui_side = SUI_ORDER_SIDE.BUY
        else:
            sui_side = SUI_ORDER_SIDE.SELL
        
        # Map order type
        if order_type == ORDER_TYPE.MARKET:
            sui_order_type = SUI_ORDER_TYPE.MARKET
        elif order_type == ORDER_TYPE.LIMIT:
            sui_order_type = SUI_ORDER_TYPE.LIMIT
        elif order_type == ORDER_TYPE.STOP_MARKET:
            sui_order_type = SUI_ORDER_TYPE.STOP_MARKET
        elif order_type == ORDER_TYPE.TAKE_PROFIT:
            sui_order_type = SUI_ORDER_TYPE.TAKE_PROFIT
        else:
            sui_order_type = SUI_ORDER_TYPE.MARKET
        
        # Create order signature request
        try:
            signature_request = {
                "symbol": symbol,
                "side": sui_side,
                "quantity": str(quantity),
                "type": sui_order_type,
                "reduceOnly": reduce_only,
                "timeInForce": time_in_force
            }
            
            # Add price for limit orders
            if price is not None and order_type != ORDER_TYPE.MARKET:
                signature_request["price"] = str(price)
            
            # Create and submit the order
            signed_order = self.client.create_signed_order(signature_request)
            response = await self.client.post_signed_order(signed_order)
            
            # Create and track the order
            order = Order(
                symbol=symbol,
                side=side,
                quantity=quantity,
                order_type=order_type,
                price=price or 0.0,
                leverage=leverage or 1,
                order_hash=signed_order.get('orderHash', ''),
                status='created'
            )
            self.orders[signed_order.get('orderHash', '')] = order
            
            return response
        except Exception as e:
            logger.error(f"Failed to place order: {str(e)}")
            raise
    
    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancel an order by ID."""
        return await self.client.cancel_order(order_id)
    
    async def close_position(self, 
                            symbol: str, 
                            quantity: Optional[float] = None) -> Dict[str, Any]:
        """Close a position for the given symbol, optionally specifying quantity."""
        positions = await self.get_positions()
        
        # Find position for the symbol
        position = next((p for p in positions if p.get("symbol") == symbol), None)
        
        if not position:
            logger.warning(f"No position found for {symbol}")
            return {"status": "error", "message": "No position found"}
        
        # Determine quantity to close
        qty_to_close = quantity or position.get("quantity", 0)
        
        # Determine side (opposite of position side)
        position_side = position.get("side")
        close_side = ORDER_SIDE.SELL if position_side == "LONG" else ORDER_SIDE.BUY
        
        # Place closing order
        return await self.place_order(
            symbol=symbol,
            side=close_side,
            quantity=qty_to_close,
            reduce_only=True
        )
    
    async def set_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        """Set leverage for a symbol."""
        try:
            result = await self.client.set_leverage(symbol, leverage)
            logger.info(f"Leverage set to {leverage} for {symbol}")
            return result
        except Exception as e:
            logger.error(f"Failed to set leverage: {str(e)}")
            raise
    
    async def get_market_price(self, symbol: str) -> float:
        """Get the current market price for a symbol."""
        orderbook = await self.client.get_orderbook(symbol)
        if orderbook and "asks" in orderbook and len(orderbook["asks"]) > 0:
            return float(orderbook["asks"][0][0])
        return 0.0
    
    async def close(self) -> None:
        """Close the client connection."""
        if self.initialized:
            await self.client.apis.close_session()
            self.initialized = False
            logger.info("Bluefin SUI client disconnected")
            
    def get_public_address(self) -> str:
        """
        Get the public address of the user.
        
        Returns:
            str: Public address of the user
        """
        return self.client.get_public_address()
        
    def get_auth_token(self) -> str:
        """
        Get the authentication token.
        
        Returns:
            str: Authentication token
        """
        return self.client.get_auth_token()
        
    def is_token_expired(self) -> bool:
        """
        Check if the authentication token is expired.
        
        Returns:
            bool: True if token is expired, False otherwise
        """
        return self.client.is_token_expired()
        
    async def refresh_auth(self) -> None:
        """Refresh the authentication token."""
        if not self.initialized:
            await self.init()
            
        await self.client.refresh_auth()

    async def order_update_handler(self, event):
        print(f"Received OrderUpdate event:")
        print(f"  Order ID: {event['orderId']}")  
        print(f"  Symbol: {event['symbol']}")
        print(f"  Status: {event['status']}")
        print(f"  Price: {event['price']}")
        print(f"  Quantity: {event['quantity']}")

    async def user_trade_handler(self, event):
        print(f"Received UserTrade event:")  
        print(f"  Symbol: {event['symbol']}")
        print(f"  Trade ID: {event['id']}")
        print(f"  Order ID: {event['orderId']}")
        print(f"  Side: {event['side']}")
        print(f"  Price: {event['price']}")  
        print(f"  Quantity: {event['qty']}")

    async def order_settlement_update_handler(self, event):
        print(f"Received OrderSettlementUpdate event:")
        print(f"  Order Hash: {event['orderHash']}")
        print(f"  Symbol: {event['symbol']}")
        print(f"  Quantity Sent for Settlement: {event['quantitySentForSettlement']}")
        print(f"  Is Maker: {event['isMaker']}")
        print(f"  Is Buy: {event['isBuy']}")
        print(f"  Average Fill Price: {event['avgFillPrice']}")
        print(f"  Fill ID: {event['fillId']}")
        print("  Matched Orders:")
        for order in event['matchedOrders']:
            print(f"    Fill Price: {order['fillPrice']}, Quantity: {order['quantity']}")
            
        # Locate the order by hash
        order = self.orders.get(event['orderHash'])
        
        if order:
            # Update the settlement status
            order.settlement_status = "sent"
            order.fill_price = float(event['avgFillPrice'])
            order.matched_quantity = float(event['quantitySentForSettlement'])
            order.is_maker = event['isMaker']
            # Save the updated order
            await self.save_order(order)
        else:
            logger.warning(f"Received settlement update for unknown order: {event['orderHash']}")
            
    async def order_requeue_update_handler(self, event):
        print(f"Received OrderRequeueUpdate event:")
        print(f"  Order Hash: {event['orderHash']}")
        print(f"  Symbol: {event['symbol']}")
        print(f"  Quantity Sent for Requeue: {event['quantitySentForRequeue']}")
        print(f"  Is Buy: {event['isBuy']}")
        print(f"  Fill ID: {event['fillId']}")
        
        # Locate the order by hash
        order = self.orders.get(event['orderHash'])
        
        if order:
            # Increment the requeue count
            self.requeue_counts[event['orderHash']] = self.requeue_counts.get(event['orderHash'], 0) + 1
            
            # Check if we need to adjust the price
            if self.requeue_counts[event['orderHash']] > REQUEUE_ADJUSTMENT_THRESHOLD:
                # Calculate new price with 1% adjustment
                adjustment = 1.01 if order.side == "BUY" else 0.99
                new_price = order.price * adjustment
                
                logger.info(f"Order {order.hash} failed settlement {self.requeue_counts[event['orderHash']]} times, adjusting price to {new_price}")
                
                # Update the order price
                order.price = new_price
                
            # Save the updated order
            await self.save_order(order)
        else:
            logger.warning(f"Received requeue update for unknown order: {event['orderHash']}")
        
    async def order_cancelled_on_reversion_handler(self, event):
        print(f"Received OrderCancelledOnReversionUpdate event:")
        print(f"  Order Hash: {event['orderHash']}")
        print(f"  Symbol: {event['symbol']}")
        print(f"  Quantity Cancelled: {event['quantitySentForCancellation']}")
        print(f"  Is Buy: {event['isBuy']}")
        print(f"  Fill ID: {event['fillId']}")
        
        # Locate the order by hash
        order = self.orders.get(event['orderHash'])
        
        if order:
            # Mark the order as cancelled
            order.cancelled = True
            
            # Save the updated order
            await self.save_order(order)
        else:
            logger.warning(f"Received cancellation update for unknown order: {event['orderHash']}")

    async def save_order(self, order):
        """Save updated order information."""
        # Find the order in our list and update it
        existing_order = self.orders.get(order.hash)
        if existing_order:
            # Update the existing order with new values
            for attr, value in vars(order).items():
                setattr(existing_order, attr, value)
        else:
            # Add the new order to our list
            self.orders[order.hash] = order
            
        # Log the order update
        logger.info(f"Order updated: {order}")
        
        # Here you could persist orders to a database if needed
        
        return order

    async def get_user_trades_history(self,
                                     symbol: Optional[str] = None,
                                     maker: Optional[bool] = None,
                                     order_type: Optional[str] = None,
                                     from_id: Optional[str] = None,
                                     start_time: Optional[int] = None,
                                     end_time: Optional[int] = None,
                                     limit: int = 50,
                                     cursor: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Get the user's completed trades history.
        
        Args:
            symbol: Market symbol for which to get trades
            maker: If True, fetch trades where the user is the maker
            order_type: Order type (Market or Limit)
            from_id: Get trades after the provided ID
            start_time: The time after which trades will be fetched from
            end_time: The time before which all trades will be returned
            limit: Total number of records to get (max 50)
            cursor: The particular page number to be fetched
            
        Returns:
            List of trade records
        """
        try:
            # Prepare parameters
            params = {}
            if symbol:
                params["symbol"] = symbol
            if maker is not None:
                params["maker"] = maker
            if order_type:
                params["orderType"] = order_type
            if from_id:
                params["fromId"] = from_id
            if start_time:
                params["startTime"] = start_time
            if end_time:
                params["endTime"] = end_time
            if limit:
                params["limit"] = limit
            if cursor:
                params["cursor"] = cursor
                
            # Call API
            trades = await self.client.get_user_trades_history(**params)
            return trades or []
        except Exception as e:
            self.logger.error(f"Error getting user trades history: {e}")
            return []

    async def initialize_websocket(self, on_open=None):
        """
        Initialize WebSocket connection to Bluefin Exchange.
        
        Args:
            on_open: Callback function to execute when connection is established
        """
        if not self.initialized:
            await self.init()
            
        # Initialize WebSocket connection
        self.client.webSocketClient.initialize_socket(on_open=on_open)
        self.logger.info("WebSocket connection initialized")
        
    async def subscribe_global_updates(self, symbol):
        """
        Subscribe to global updates for a specific symbol.
        
        Args:
            symbol: Market symbol to subscribe to
            
        Returns:
            bool: True if subscription was successful
        """
        if not self.initialized:
            await self.init()
            
        resp = self.client.webSocketClient.subscribe_global_updates_by_symbol(symbol=symbol)
        if resp:
            self.logger.info(f"Subscribed to global updates for {symbol}")
        return resp
        
    async def subscribe_user_updates(self):
        """
        Subscribe to user-specific updates.
        
        Returns:
            bool: True if subscription was successful
        """
        if not self.initialized:
            await self.init()
            
        resp = self.client.webSocketClient.subscribe_user_update_by_token()
        if resp:
            self.logger.info("Subscribed to user updates")
        return resp
        
    async def listen_for_events(self, event_type, callback):
        """
        Listen for specific events and trigger callback when received.
        
        Args:
            event_type: Type of event to listen for (from SOCKET_EVENTS)
            callback: Function to call when event is received
        """
        if not self.initialized:
            await self.init()
            
        self.client.webSocketClient.listen(event_type, callback)
        self.logger.info(f"Listening for {event_type} events")
        
    async def unsubscribe_global_updates(self, symbol):
        """
        Unsubscribe from global updates for a specific symbol.
        
        Args:
            symbol: Market symbol to unsubscribe from
            
        Returns:
            bool: True if unsubscription was successful
        """
        if not self.initialized:
            await self.init()
            
        resp = self.client.webSocketClient.unsubscribe_global_updates_by_symbol(symbol=symbol)
        if resp:
            self.logger.info(f"Unsubscribed from global updates for {symbol}")
        return resp
        
    async def stop_websocket(self):
        """Stop the WebSocket connection."""
        if not self.initialized:
            return
            
        self.client.webSocketClient.stop()
        self.logger.info("WebSocket connection stopped")

    async def get_orderbook(self, symbol: str) -> dict:
        """
        Get the current orderbook for a symbol.
        
        Args:
            symbol: Market symbol to get orderbook for
            
        Returns:
            dict: Orderbook data with bids and asks
        """
        if not self.initialized:
            await self.init()
            
        return await self.client.get_orderbook(symbol)
        
    async def get_recent_trades(self, symbol: str) -> list:
        """
        Get recent trades for a symbol.
        
        Args:
            symbol: Market symbol to get trades for
            
        Returns:
            list: Recent trades data
        """
        if not self.initialized:
            await self.init()
            
        return await self.client.get_recent_trades(symbol)
        
    async def get_candles(self, symbol: str, interval: str = "1h", limit: int = 100) -> list:
        """
        Get candlestick data for a symbol.
        
        Args:
            symbol: Market symbol to get candles for
            interval: Time interval for candles (e.g., "1m", "5m", "1h", "1d")
            limit: Number of candles to retrieve
            
        Returns:
            list: Candlestick data
        """
        if not self.initialized:
            await self.init()
            
        return await self.client.get_candles(symbol, interval, limit)
        
    async def get_exchange_info(self) -> dict:
        """
        Get exchange information including trading pairs, limits, etc.
        
        Returns:
            dict: Exchange information
        """
        if not self.initialized:
            await self.init()
            
        return await self.client.get_exchange_info()

    async def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get a specific position by symbol.
        
        Args:
            symbol: Market symbol to get position for
            
        Returns:
            dict or None: Position data if exists, None otherwise
        """
        if not self.initialized:
            await self.init()
            
        positions = await self.get_positions()
        for position in positions:
            if position["symbol"] == symbol:
                return position
        return None
        
    async def get_funding_payments(self) -> List[Dict[str, Any]]:
        """
        Get funding payments history.
        
        Returns:
            list: Funding payments data
        """
        if not self.initialized:
            await self.init()
            
        return await self.client.get_funding_payments()
        
    async def get_funding_rate(self, symbol: str) -> float:
        """
        Get current funding rate for a symbol.
        
        Args:
            symbol: Market symbol to get funding rate for
            
        Returns:
            float: Current funding rate
        """
        if not self.initialized:
            await self.init()
            
        funding_info = await self.client.get_funding_info(symbol)
        return float(funding_info.get("fundingRate", 0))

    async def get_order_history(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get order history for a symbol.
        
        Args:
            symbol: Market symbol to get order history for
            
        Returns:
            list: Order history data
        """
        if not self.initialized:
            await self.init()
            
        return await self.client.get_order_history(symbol) or []
        
    async def get_trade_history(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get trade history for a symbol.
        
        Args:
            symbol: Market symbol to get trade history for
            
        Returns:
            list: Trade history data
        """
        if not self.initialized:
            await self.init()
            
        return await self.client.get_trade_history(symbol) or []

class BluefinApiClient(BluefinClientInterface):
    """Client implementation for Bluefin Exchange using API key authentication."""
    
    def __init__(self, api_key: str, api_secret: str, network: str = "MAINNET", api_url: Optional[str] = None):
        """Initialize the Bluefin API client."""
        self.api_key = api_key
        self.api_secret = api_secret
        self.network = network
        self.api_url = api_url
        
        # Initialize the client
        self.client = ApiClient(
            api_key=self.api_key,
            api_secret=self.api_secret,
            base_url=self.api_url
        )
        
        # Set up logging
        self.logger = logging.getLogger(__name__)
    
    async def get_account_info(self) -> Dict[str, Any]:
        """Get account information including balance and positions."""
        account_info = await self.client.get_account_info()
        return account_info
    
    async def get_positions(self) -> List[Dict[str, Any]]:
        """Get current positions."""
        return await self.client.get_positions()
    
    async def place_order(self, 
                         symbol: str, 
                         side: str, 
                         quantity: float, 
                         price: Optional[float] = None,
                         order_type: str = ORDER_TYPE.MARKET,
                         reduce_only: bool = False,
                         time_in_force: str = "GTC",
                         leverage: Optional[int] = None) -> Dict[str, Any]:
        """Place an order on Bluefin Exchange."""
        # Set leverage if provided
        if leverage is not None:
            await self.set_leverage(symbol, leverage)
        
        # Convert order parameters
        order_params = {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "type": order_type,
            "reduceOnly": reduce_only,
            "timeInForce": time_in_force
        }
        
        # Add price for limit orders
        if price is not None and order_type != ORDER_TYPE.MARKET:
            order_params["price"] = price
        
        # Place the order
        return await self.client.place_order(**order_params)
    
    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancel an order by ID."""
        return await self.client.cancel_order(order_id)
    
    async def close_position(self, 
                            symbol: str, 
                            quantity: Optional[float] = None) -> Dict[str, Any]:
        """Close a position for the given symbol, optionally specifying quantity."""
        return await self.client.close_position(symbol, quantity)
    
    async def set_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        """Set leverage for a symbol."""
        return await self.client.set_leverage(symbol, leverage)
    
    async def get_market_price(self, symbol: str) -> float:
        """Get current market price for a symbol."""
        market_data = await self.client.get_market_data(symbol)
        return float(market_data.get("markPrice", 0))
    
    async def close(self) -> None:
        """Close the client connection."""
        await self.client.close_session()
        logger.info("Bluefin API client closed")

    async def get_user_trades_history(self,
                                     symbol: Optional[str] = None,
                                     maker: Optional[bool] = None,
                                     order_type: Optional[str] = None,
                                     from_id: Optional[str] = None,
                                     start_time: Optional[int] = None,
                                     end_time: Optional[int] = None,
                                     limit: int = 50,
                                     cursor: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Get the user's completed trades history.
        
        Args:
            symbol: Market symbol for which to get trades
            maker: If True, fetch trades where the user is the maker
            order_type: Order type (Market or Limit)
            from_id: Get trades after the provided ID
            start_time: The time after which trades will be fetched from
            end_time: The time before which all trades will be returned
            limit: Total number of records to get (max 50)
            cursor: The particular page number to be fetched
            
        Returns:
            List of trade records
        """
        try:
            # Build query parameters
            params = {}
            if symbol:
                params["symbol"] = symbol
            if maker is not None:
                params["maker"] = str(maker).lower()
            if order_type:
                params["orderType"] = order_type
            if from_id:
                params["fromid"] = from_id
            if start_time:
                params["startTime"] = start_time
            if end_time:
                params["endTime"] = end_time
            if limit:
                params["limit"] = min(limit, 50)  # Ensure limit doesn't exceed 50
            if cursor:
                params["cursor"] = cursor
                
            # Call the getUserTradesHistory method
            trades = await self.client.get_user_trades_history(**params)
            return trades
        except Exception as e:
            self.logger.error(f"Failed to get user trades history: {e}")
            return []

class MockBluefinClient(BluefinClientInterface):
    """Mock client for testing without connecting to Bluefin Exchange."""
    
    def __init__(self):
        """Initialize the mock client."""
        self.positions = []
        self.orders = []
        self.trades = []
        self.account_balance = 10000.0  # Default balance
        self.logger = logging.getLogger(__name__)
    
    async def get_account_info(self) -> Dict[str, Any]:
        """Get mock account information."""
        return {
            "balance": self.account_balance,
            "availableMargin": self.account_balance * 0.9,
            "positions": self.positions
        }
    
    async def get_positions(self) -> List[Dict[str, Any]]:
        """Get mock positions."""
        return self.positions
    
    async def place_order(self, 
                         symbol: str, 
                         side: str, 
                         quantity: float, 
                         price: Optional[float] = None,
                         order_type: str = ORDER_TYPE.MARKET,
                         reduce_only: bool = False,
                         time_in_force: str = "GTC",
                         leverage: Optional[int] = None) -> Dict[str, Any]:
        """Place a mock order."""
        # Generate mock price if not provided
        if price is None:
            price = 100.0  # Mock price
        
        # Create mock order
        order_id = f"order-{len(self.orders) + 1}"
        self.orders.append({
            "id": order_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "type": order_type,
            "reduceOnly": reduce_only,
            "timeInForce": time_in_force,
            "status": "FILLED",
            "timestamp": int(time.time())
        })
        
        # If it's a market order and not reduce_only, create a position
        if order_type == ORDER_TYPE.MARKET and not reduce_only:
            position_id = f"position-{len(self.positions) + 1}"
            
            position = {
                "id": position_id,
                "symbol": symbol,
                "side": "LONG" if side == ORDER_SIDE.BUY else "SHORT",
                "entryPrice": price,
                "markPrice": price,
                "quantity": quantity,
                "leverage": leverage or 5,
                "unrealizedPnl": 0.0,
                "marginType": "ISOLATED"
            }
            
            # Update existing position or add new one
            existing_position = next((p for p in self.positions if p["symbol"] == symbol), None)
            if existing_position:
                # Update existing position
                if existing_position["side"] == position["side"]:
                    # Adding to position
                    total_qty = existing_position["quantity"] + quantity
                    avg_price = ((existing_position["entryPrice"] * existing_position["quantity"]) + 
                               (price * quantity)) / total_qty
                    existing_position["quantity"] = total_qty
                    existing_position["entryPrice"] = avg_price
                else:
                    # Reducing position
                    net_qty = existing_position["quantity"] - quantity
                    if net_qty > 0:
                        existing_position["quantity"] = net_qty
                    elif net_qty < 0:
                        # Position flipped sides
                        existing_position["side"] = position["side"]
                        existing_position["quantity"] = abs(net_qty)
                        existing_position["entryPrice"] = price
                    else:
                        # Position closed
                        self.positions.remove(existing_position)
            else:
                # Add new position
                self.positions.append(position)
        
        # If it's a reduce_only order, reduce or close the position
        elif reduce_only:
            existing_position = next((p for p in self.positions if p["symbol"] == symbol), None)
            if existing_position:
                # Calculate new quantity
                new_qty = existing_position["quantity"] - quantity
                if new_qty <= 0:
                    # Close position
                    self.positions.remove(existing_position)
                else:
                    # Reduce position
                    existing_position["quantity"] = new_qty
        
        return self.orders[-1]
    
    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """Cancel a mock order."""
        for order in self.orders:
            if order["id"] == order_id:
                order["status"] = "CANCELED"
                return {"id": order_id, "status": "CANCELED"}
        
        return {"error": "Order not found"}
    
    async def close_position(self, 
                            symbol: str, 
                            quantity: Optional[float] = None) -> Dict[str, Any]:
        """Close a mock position."""
        position = next((p for p in self.positions if p["symbol"] == symbol), None)
        
        if not position:
            return {"error": "Position not found"}
        
        qty_to_close = quantity or position["quantity"]
        side = ORDER_SIDE.SELL if position["side"] == "LONG" else ORDER_SIDE.BUY
        
        return await self.place_order(
            symbol=symbol,
            side=side,
            quantity=qty_to_close,
            reduce_only=True
        )
    
    async def set_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        """Set mock leverage."""
        for position in self.positions:
            if position["symbol"] == symbol:
                position["leverage"] = leverage
        
        return {"symbol": symbol, "leverage": leverage}
    
    async def get_market_price(self, symbol: str) -> float:
        """Get mock market price."""
        return 100.0  # Mock price
    
    async def close(self) -> None:
        """Close the mock client."""
        logger.info("Mock Bluefin client closed")

    async def get_user_trades_history(self,
                                     symbol: Optional[str] = None,
                                     maker: Optional[bool] = None,
                                     order_type: Optional[str] = None,
                                     from_id: Optional[str] = None,
                                     start_time: Optional[int] = None,
                                     end_time: Optional[int] = None,
                                     limit: int = 50,
                                     cursor: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Get the user's completed trades history.
        
        Args:
            symbol: Market symbol for which to get trades
            maker: If True, fetch trades where the user is the maker
            order_type: Order type (Market or Limit)
            from_id: Get trades after the provided ID
            start_time: The time after which trades will be fetched from
            end_time: The time before which all trades will be returned
            limit: Total number of records to get (max 50)
            cursor: The particular page number to be fetched
            
        Returns:
            List of trade records
        """
        # Create mock trades if none exist
        if not self.trades:
            self.trades = [
                {
                    "id": f"trade_{i}",
                    "symbol": "SUI-PERP",
                    "side": "BUY" if i % 2 == 0 else "SELL",
                    "size": 10.0,
                    "price": 1.0 + (i * 0.01),
                    "timestamp": int(time.time()) - (i * 3600),
                    "maker": i % 3 == 0,
                    "orderType": "MARKET" if i % 2 == 0 else "LIMIT"
                }
                for i in range(1, 21)  # Create 20 mock trades
            ]
        
        # Filter trades based on parameters
        filtered_trades = self.trades.copy()
        
        if symbol:
            filtered_trades = [t for t in filtered_trades if t["symbol"] == symbol]
        
        if maker is not None:
            filtered_trades = [t for t in filtered_trades if t["maker"] == maker]
        
        if order_type:
            filtered_trades = [t for t in filtered_trades if t["orderType"] == order_type]
        
        if from_id:
            # Find the index of the trade with from_id
            try:
                from_index = next(i for i, t in enumerate(filtered_trades) if t["id"] == from_id)
                filtered_trades = filtered_trades[from_index + 1:]
            except StopIteration:
                pass
        
        if start_time:
            filtered_trades = [t for t in filtered_trades if t["timestamp"] >= start_time]
        
        if end_time:
            filtered_trades = [t for t in filtered_trades if t["timestamp"] <= end_time]
        
        # Apply pagination
        total_count = len(filtered_trades)
        page_size = min(limit, 50)
        
        if cursor is not None:
            start_idx = cursor * page_size
            end_idx = start_idx + page_size
            filtered_trades = filtered_trades[start_idx:end_idx]
        else:
            filtered_trades = filtered_trades[:page_size]
        
        return {
            "trades": filtered_trades,
            "total": total_count,
            "page": cursor or 0,
            "pageSize": page_size
        }

async def create_bluefin_client(
    use_mock: bool = False,
    private_key: Optional[str] = None,
    api_key: Optional[str] = None,
    api_secret: Optional[str] = None,
    network: str = "MAINNET",
    api_url: Optional[str] = None
) -> BluefinClientInterface:
    """
    Create and initialize a Bluefin client based on available credentials.
    
    Args:
        use_mock: Whether to use a mock client (for testing)
        private_key: Bluefin private key (for SUI network)
        api_key: Bluefin API key
        api_secret: Bluefin API secret
        network: Network to connect to (MAINNET or TESTNET)
        api_url: Custom API URL (for v2 client)
        
    Returns:
        Initialized Bluefin client
    """
    if use_mock:
        logger.info("Creating mock Bluefin client")
        return MockBluefinClient()
    
    # Check environment variables for credentials if not provided
    private_key = private_key or os.environ.get("BLUEFIN_PRIVATE_KEY")
    api_key = api_key or os.environ.get("BLUEFIN_API_KEY")
    api_secret = api_secret or os.environ.get("BLUEFIN_API_SECRET")
    network = network or os.environ.get("BLUEFIN_NETWORK", "MAINNET")
    api_url = api_url or os.environ.get("BLUEFIN_API_URL")
    
    # Try to create SUI client if private key is available
    if private_key:
        try:
            logger.info("Creating Bluefin SUI client")
            client = BluefinSuiClient(private_key=private_key, network=network)
            await client.init()
            return client
        except Exception as e:
            logger.error(f"Failed to initialize Bluefin SUI client: {str(e)}")
            if not (api_key and api_secret):
                raise
    
    # Try to create API client if API credentials are available
    if api_key and api_secret:
        try:
            logger.info("Creating Bluefin API client")
            return BluefinApiClient(api_key=api_key, api_secret=api_secret, network=network)
        except Exception as e:
            logger.error(f"Failed to initialize Bluefin API client: {str(e)}")
            raise
    
    # If we get here and no clients could be created, raise an error
    raise ValueError(
        "Unable to create Bluefin client. Please provide either a private key "
        "or API key and secret, or set them in environment variables."
    ) 