"""
Order Manager — Smart Order Routing & Execution
================================================
Handles all order submission, tracking, and cancellation.

Key principles:
  1. NEVER use market orders on options (extreme slippage risk)
  2. Always use limit orders, starting at mid-price
  3. Gradual aggression: step toward market only if needed
  4. Track all fills for slippage analytics
  5. Duplicate order protection (idempotent submissions)
  6. Timeout unfilled orders — don't let them become stale
  7. Verify fills before updating position tracker

Execution flow for each order:
  Submit limit @ mid → Wait N seconds → Step bps toward market
  → Wait N seconds → Step again → Max aggression reached → Cancel
"""

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Optional, Dict, List
import structlog

log = structlog.get_logger(__name__)


class OrderStatus(Enum):
    PENDING = auto()
    SUBMITTED = auto()
    PARTIAL = auto()
    FILLED = auto()
    CANCELLED = auto()
    REJECTED = auto()
    EXPIRED = auto()


@dataclass
class OrderResult:
    """Result of an order submission attempt."""
    order_id: str
    status: OrderStatus
    contracts_requested: int
    contracts_filled: int
    avg_fill_price: float
    submitted_at: datetime
    filled_at: Optional[datetime]
    slippage_bps: float = 0.0
    rejection_reason: Optional[str] = None

    @property
    def filled(self) -> bool:
        return self.contracts_filled > 0

    @property
    def fully_filled(self) -> bool:
        return self.contracts_filled == self.contracts_requested


@dataclass
class PendingOrder:
    """Tracks an in-flight order."""
    order_id: str
    position_id: Optional[str]
    symbol: str
    structure: str
    direction: str
    contracts: int
    limit_price: float
    mid_price: float
    submitted_at: datetime
    max_aggression_bps: float
    retry_count: int = 0
    status: OrderStatus = OrderStatus.PENDING
    fill_price: float = 0.0
    contracts_filled: int = 0


class OrderManager:
    """
    Smart order routing and execution management.
    """

    def __init__(self, config, risk_guardian):
        self.config = config
        self.risk_guardian = risk_guardian
        self._pending_orders: Dict[str, PendingOrder] = {}
        self._fill_history: List[OrderResult] = []
        self._broker = None  # Injected broker adapter
        self._connected = False

        # Idempotency tracking — prevents duplicate orders
        self._submitted_signatures: set = set()

        # Slippage tracking
        self._total_slippage_bps = 0.0
        self._fill_count = 0

    async def connect(self):
        """Connect to broker."""
        # Instantiate broker adapter based on config
        # self._broker = IBKRAdapter(config) or TastyTradeAdapter(config)
        self._connected = True
        log.info("order_manager.connected")

    async def disconnect(self):
        """Disconnect from broker."""
        self._connected = False
        log.info("order_manager.disconnected")

    async def submit(self, sized_trade) -> OrderResult:
        """
        Submit a new trade. Uses gradual limit order aggression.

        This is the primary entry point for new positions.
        """
        order_id = str(uuid.uuid4())[:8]

        # Idempotency check — prevent duplicate submissions
        signature = self._build_signature(sized_trade)
        if signature in self._submitted_signatures:
            log.warning("order_manager.duplicate_detected",
                        signature=signature)
            return OrderResult(
                order_id=order_id,
                status=OrderStatus.REJECTED,
                contracts_requested=sized_trade.contracts,
                contracts_filled=0,
                avg_fill_price=0.0,
                submitted_at=datetime.now(),
                filled_at=None,
                rejection_reason="Duplicate order detected"
            )

        self._submitted_signatures.add(signature)

        # Get current mid price for the structure
        mid_price = await self._get_structure_mid_price(sized_trade)
        if mid_price <= 0:
            log.error("order_manager.invalid_mid_price",
                      trade=sized_trade.structure)
            return self._rejected_result(order_id, sized_trade, "Invalid mid price")

        # Fat-finger check on price
        sanity_ok = await self._price_sanity_check(sized_trade, mid_price)
        if not sanity_ok:
            return self._rejected_result(order_id, sized_trade, "Price sanity check failed")

        # Start at mid-price
        limit_price = mid_price
        max_aggression_bps = self.config.max_aggression_bps

        pending = PendingOrder(
            order_id=order_id,
            position_id=None,
            symbol=sized_trade.underlying,
            structure=sized_trade.structure,
            direction=sized_trade.direction,
            contracts=sized_trade.contracts,
            limit_price=limit_price,
            mid_price=mid_price,
            submitted_at=datetime.now(),
            max_aggression_bps=max_aggression_bps,
        )

        self._pending_orders[order_id] = pending

        log.info("order_manager.submitting",
                 order_id=order_id,
                 structure=sized_trade.structure,
                 contracts=sized_trade.contracts,
                 limit_price=limit_price,
                 mid=mid_price)

        # Submit with retry logic
        result = await self._submit_with_aggression(pending, sized_trade)

        # Track slippage
        if result.filled:
            slippage = result.avg_fill_price - mid_price
            slippage_bps = (slippage / mid_price) * 10000
            result.slippage_bps = slippage_bps
            self._update_slippage_stats(slippage_bps)

        self._fill_history.append(result)
        return result

    async def _submit_with_aggression(
        self, pending: PendingOrder, sized_trade
    ) -> OrderResult:
        """
        Submit limit order, stepping toward market if unfilled.
        """
        retry_interval = self.config.retry_interval_seconds
        max_retries = self.config.max_retries
        cancel_after = self.config.cancel_unfilled_after
        step_bps = self.config.limit_offset_bps

        for attempt in range(max_retries + 1):
            # Calculate current limit price
            # Direction: for debits we want to pay less, for credits receive more
            is_debit = pending.structure in ["debit_spread", "long_straddle"]
            aggression_bps = step_bps * attempt

            if is_debit:
                # Buying: start at mid, step up (become more aggressive)
                limit_price = pending.mid_price * (1 + aggression_bps / 10000)
            else:
                # Selling: start at mid, step down
                limit_price = pending.mid_price * (1 - aggression_bps / 10000)

            # Don't exceed max aggression
            max_deviation = pending.mid_price * (pending.max_aggression_bps / 10000)
            if abs(limit_price - pending.mid_price) > max_deviation:
                log.warning("order_manager.max_aggression_reached",
                            order_id=pending.order_id,
                            mid=pending.mid_price,
                            limit=limit_price)
                await self._cancel_order(pending)
                return self._cancelled_result(pending)

            # Submit/modify order
            broker_result = await self._broker_submit(
                pending=pending,
                limit_price=limit_price,
                attempt=attempt,
            )

            if broker_result and broker_result.get('filled'):
                fill_price = broker_result['fill_price']
                contracts_filled = broker_result['contracts_filled']

                log.info("order_manager.filled",
                         order_id=pending.order_id,
                         contracts=contracts_filled,
                         fill_price=fill_price,
                         attempt=attempt)

                return OrderResult(
                    order_id=pending.order_id,
                    status=OrderStatus.FILLED,
                    contracts_requested=pending.contracts,
                    contracts_filled=contracts_filled,
                    avg_fill_price=fill_price,
                    submitted_at=pending.submitted_at,
                    filled_at=datetime.now(),
                )

            if attempt < max_retries:
                log.debug("order_manager.retry",
                          order_id=pending.order_id,
                          attempt=attempt + 1,
                          limit_price=limit_price)
                await asyncio.sleep(retry_interval)

        # All attempts failed — cancel
        await self._cancel_order(pending)
        return self._cancelled_result(pending)

    async def close_position(self, position, aggressive: bool = False) -> OrderResult:
        """
        Close an existing position.

        aggressive=True: Use wider price tolerance for emergency closes.
        """
        order_id = str(uuid.uuid4())[:8]

        log.info("order_manager.closing_position",
                 order_id=order_id,
                 position_id=position.id,
                 contracts=position.contracts,
                 aggressive=aggressive)

        if aggressive:
            # In emergency: accept higher slippage to guarantee exit
            # Still use limit, but very aggressive relative to mid
            pass

        # Implementation: reverse the entry structure
        result = await self._submit_closing_order(position, aggressive)

        if not result.filled:
            log.critical("order_manager.close_failed",
                         position_id=position.id,
                         reason="Could not fill closing order")
            # This requires immediate human attention
            # Alert sent by strategy._defensive_close fallback

        return result

    async def partial_close(self, position, contracts_to_close: int) -> OrderResult:
        """Partially close a position."""
        order_id = str(uuid.uuid4())[:8]
        log.info("order_manager.partial_close",
                 order_id=order_id,
                 position_id=position.id,
                 closing=contracts_to_close,
                 remaining=position.contracts - contracts_to_close)

        return await self._submit_partial_closing_order(position, contracts_to_close)

    async def hedge_delta(self, position):
        """
        Submit underlying hedge to neutralize delta.
        Uses shares/ETF (SPY, QQQ) not futures.
        """
        delta = position.delta
        shares_to_hedge = round(abs(delta) * position.contracts * 100)

        if shares_to_hedge < 1:
            return

        direction = "sell" if delta > 0 else "buy"
        log.info("order_manager.delta_hedge",
                 position_id=position.id,
                 delta=delta,
                 shares=shares_to_hedge,
                 direction=direction)

        # Submit market order for underlying (acceptable for hedging)
        # Small size relative to underlying liquidity
        await self._submit_underlying_order(
            symbol=position.underlying,
            direction=direction,
            shares=shares_to_hedge,
        )

    # ──────────────────────────────────────────────────────────────
    # ANALYTICS
    # ──────────────────────────────────────────────────────────────

    def get_fill_analytics(self) -> dict:
        """Return fill quality analytics."""
        if not self._fill_history:
            return {}

        filled = [r for r in self._fill_history if r.filled]
        if not filled:
            return {"fills": 0, "fill_rate": 0}

        total = len(self._fill_history)
        avg_slippage = sum(r.slippage_bps for r in filled) / len(filled)

        return {
            "total_orders": total,
            "filled": len(filled),
            "fill_rate": len(filled) / total,
            "avg_slippage_bps": avg_slippage,
            "total_slippage_bps": self._total_slippage_bps,
        }

    def _update_slippage_stats(self, slippage_bps: float):
        """Track slippage statistics."""
        self._total_slippage_bps += slippage_bps
        self._fill_count += 1

    # ──────────────────────────────────────────────────────────────
    # STUBS (implement with broker adapter)
    # ──────────────────────────────────────────────────────────────

    async def _broker_submit(self, pending, limit_price, attempt) -> Optional[dict]:
        """Submit order to broker. Implement in broker adapter."""
        raise NotImplementedError("Implement with broker adapter (IBKR/TastyTrade)")

    async def _cancel_order(self, pending: PendingOrder):
        """Cancel an open order."""
        raise NotImplementedError("Implement cancel order")

    async def _submit_closing_order(self, position, aggressive: bool) -> OrderResult:
        """Submit order to close position."""
        raise NotImplementedError("Implement closing order")

    async def _submit_partial_closing_order(self, position, contracts) -> OrderResult:
        """Submit partial closing order."""
        raise NotImplementedError("Implement partial close order")

    async def _submit_underlying_order(self, symbol, direction, shares):
        """Submit underlying share order for delta hedge."""
        raise NotImplementedError("Implement underlying order for delta hedge")

    async def _get_structure_mid_price(self, sized_trade) -> float:
        """Get current mid-price for a multi-leg structure."""
        raise NotImplementedError("Implement structure pricing from live chain")

    async def _price_sanity_check(self, sized_trade, price: float) -> bool:
        """Validate price is within sanity bounds."""
        model_price = getattr(sized_trade, 'model_price', None)
        if not model_price:
            return True  # Can't check, pass through

        deviation = abs(price - model_price) / model_price
        return deviation <= self.risk_limits.execution_risk.price_sanity_check_pct

    def _build_signature(self, sized_trade) -> str:
        """Build idempotency signature for order deduplication."""
        return (f"{sized_trade.underlying}:{sized_trade.structure}:"
                f"{sized_trade.direction}:{sized_trade.contracts}:"
                f"{sized_trade.long_strike}:{sized_trade.short_strike}")

    def _rejected_result(self, order_id, sized_trade, reason) -> OrderResult:
        return OrderResult(
            order_id=order_id,
            status=OrderStatus.REJECTED,
            contracts_requested=sized_trade.contracts,
            contracts_filled=0,
            avg_fill_price=0.0,
            submitted_at=datetime.now(),
            filled_at=None,
            rejection_reason=reason,
        )

    def _cancelled_result(self, pending: PendingOrder) -> OrderResult:
        return OrderResult(
            order_id=pending.order_id,
            status=OrderStatus.CANCELLED,
            contracts_requested=pending.contracts,
            contracts_filled=pending.contracts_filled,
            avg_fill_price=pending.fill_price,
            submitted_at=pending.submitted_at,
            filled_at=None,
        )
