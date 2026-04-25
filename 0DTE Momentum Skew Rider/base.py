"""
Broker Adapter Base Class
=========================
Abstract interface for all broker integrations.
Implement this for each broker: IBKR, TastyTrade, Tradier, etc.

This separation allows:
  - Swapping brokers without changing strategy logic
  - Paper trading via mock adapter
  - A/B testing execution quality across brokers
  - Regulatory compliance per-broker
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, List, Dict
from datetime import datetime


@dataclass
class OptionQuote:
    """Live option quote from broker."""
    symbol: str
    strike: float
    expiry: str
    option_type: str     # "call" | "put"
    bid: float
    ask: float
    last: float
    mid: float
    volume: int
    open_interest: int
    iv: float            # Implied volatility
    delta: float
    gamma: float
    theta: float
    vega: float
    timestamp: datetime


@dataclass
class OptionChain:
    """Full options chain for an underlying."""
    underlying: str
    underlying_price: float
    timestamp: datetime
    expirations: List[str]
    calls: Dict[str, List[OptionQuote]]  # expiry -> quotes
    puts: Dict[str, List[OptionQuote]]   # expiry -> quotes


@dataclass
class AccountInfo:
    """Broker account state."""
    account_id: str
    net_liquidation: float
    cash_balance: float
    buying_power: float
    option_buying_power: float
    day_trades_remaining: int    # PDT rule tracking


@dataclass
class BrokerFill:
    """Confirmed order fill from broker."""
    order_id: str
    fill_id: str
    symbol: str
    contracts: int
    fill_price: float
    commission: float
    timestamp: datetime
    exchange: str


class BrokerAdapter(ABC):
    """
    Abstract broker adapter interface.
    All broker-specific code lives in concrete implementations.
    """

    @abstractmethod
    async def connect(self) -> bool:
        """Establish connection to broker API."""

    @abstractmethod
    async def disconnect(self):
        """Clean disconnection."""

    @abstractmethod
    async def get_account_info(self) -> AccountInfo:
        """Get current account state."""

    @abstractmethod
    async def get_option_chain(
        self, symbol: str, expiry: Optional[str] = None
    ) -> OptionChain:
        """Fetch options chain for an underlying."""

    @abstractmethod
    async def get_quote(
        self, symbol: str, strike: float, expiry: str, option_type: str
    ) -> OptionQuote:
        """Get quote for a specific option."""

    @abstractmethod
    async def submit_limit_order(
        self,
        symbol: str,
        option_type: str,
        strike: float,
        expiry: str,
        action: str,        # "buy" | "sell"
        contracts: int,
        limit_price: float,
        order_ref: str,     # Your internal order ID
    ) -> str:
        """Submit a limit order. Returns broker order ID."""

    @abstractmethod
    async def submit_multi_leg_order(
        self,
        legs: List[dict],   # List of {symbol, strike, expiry, type, action, contracts}
        limit_price: float,
        order_ref: str,
    ) -> str:
        """Submit a multi-leg combo order (spread, condor)."""

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> bool:
        """Cancel an open order."""

    @abstractmethod
    async def get_order_status(self, broker_order_id: str) -> dict:
        """Get current status of an order."""

    @abstractmethod
    async def get_positions(self) -> List[dict]:
        """Get all open positions."""

    @abstractmethod
    async def get_vix(self) -> float:
        """Get current VIX level."""

    @abstractmethod
    async def get_underlying_price(self, symbol: str) -> float:
        """Get current underlying price."""

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        """Return connection status."""

    @property
    @abstractmethod
    def supports_0dte(self) -> bool:
        """Whether this broker supports 0DTE trading."""

    @property
    @abstractmethod
    def supports_multi_leg(self) -> bool:
        """Whether this broker supports multi-leg combo orders."""


class MockPaperBroker(BrokerAdapter):
    """
    Paper trading mock adapter for testing.
    Simulates realistic fills with configurable slippage.
    """

    def __init__(self, starting_capital: float = 100_000):
        self._capital = starting_capital
        self._positions = []
        self._orders = {}
        self._connected = False
        self._slippage_bps = 1.5  # Simulate 1.5bps average slippage

    async def connect(self) -> bool:
        self._connected = True
        return True

    async def disconnect(self):
        self._connected = False

    async def get_account_info(self) -> AccountInfo:
        return AccountInfo(
            account_id="PAPER-001",
            net_liquidation=self._capital,
            cash_balance=self._capital,
            buying_power=self._capital * 2,
            option_buying_power=self._capital * 0.5,
            day_trades_remaining=999,  # No PDT in paper
        )

    async def get_option_chain(self, symbol, expiry=None) -> OptionChain:
        raise NotImplementedError("Connect to real data source for option chain")

    async def get_quote(self, symbol, strike, expiry, option_type) -> OptionQuote:
        raise NotImplementedError("Connect to real data source for quotes")

    async def submit_limit_order(self, symbol, option_type, strike, expiry,
                                  action, contracts, limit_price, order_ref) -> str:
        """Simulate order fill with realistic slippage."""
        import uuid
        broker_id = str(uuid.uuid4())[:8]

        # Simulate slippage
        slippage_multiplier = 1 + (self._slippage_bps / 10000)
        if action == "buy":
            fill_price = limit_price * slippage_multiplier
        else:
            fill_price = limit_price / slippage_multiplier

        self._orders[broker_id] = {
            "status": "filled",
            "fill_price": fill_price,
            "contracts_filled": contracts,
            "order_ref": order_ref,
        }

        return broker_id

    async def submit_multi_leg_order(self, legs, limit_price, order_ref) -> str:
        import uuid
        broker_id = str(uuid.uuid4())[:8]
        self._orders[broker_id] = {
            "status": "filled",
            "fill_price": limit_price,
            "contracts_filled": legs[0]["contracts"] if legs else 0,
        }
        return broker_id

    async def cancel_order(self, broker_order_id) -> bool:
        if broker_order_id in self._orders:
            self._orders[broker_order_id]["status"] = "cancelled"
            return True
        return False

    async def get_order_status(self, broker_order_id) -> dict:
        return self._orders.get(broker_order_id, {"status": "unknown"})

    async def get_positions(self) -> List[dict]:
        return self._positions

    async def get_vix(self) -> float:
        # Return realistic VIX — implement with real data in production
        return 18.5  # Default for paper trading

    async def get_underlying_price(self, symbol) -> float:
        raise NotImplementedError("Connect to real data source for underlying price")

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def supports_0dte(self) -> bool:
        return True

    @property
    def supports_multi_leg(self) -> bool:
        return True
