from __future__ import annotations
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

@dataclass
class Transaction:
    asset: str
    asset_type: str
    quantity: float
    price: float
    side: str  # 'buy' or 'sell'
    date: datetime

@dataclass
class BuyLot:
    quantity: float
    price: float

@dataclass
class AssetPosition:
    asset: str
    asset_type: str
    lots: List[BuyLot] = field(default_factory=list)
    realized_pnl: float = 0.0
    current_price: float = 0.0

    def add_transaction(self, transaction: Transaction) -> None:
        if transaction.side == "buy":
            self.lots.append(BuyLot(quantity=transaction.quantity, price=transaction.price))
        elif transaction.side == "sell":
            self._apply_sell(transaction.quantity, transaction.price)
        else:
            raise ValueError("Transaction side must be 'buy' or 'sell'")

    def _apply_sell(self, quantity: float, price: float) -> None:
        remaining = quantity
        quantity_on_hand = self.quantity_on_hand
        if remaining > quantity_on_hand + 1e-9:
            raise ValueError(f"Không đủ giữ lượng để bán: {self.asset} ({quantity_on_hand} hiện có)")

        new_lots: List[BuyLot] = []
        for lot in self.lots:
            if remaining <= 0:
                new_lots.append(lot)
                continue
            used = min(lot.quantity, remaining)
            self.realized_pnl += used * (price - lot.price)
            remaining -= used
            leftover = lot.quantity - used
            if leftover > 1e-12:
                new_lots.append(BuyLot(quantity=leftover, price=lot.price))
        self.lots = new_lots

    @property
    def quantity_on_hand(self) -> float:
        return sum(lot.quantity for lot in self.lots)

    @property
    def total_cost(self) -> float:
        return sum(lot.quantity * lot.price for lot in self.lots)

    @property
    def average_cost(self) -> float:
        qty = self.quantity_on_hand
        return self.total_cost / qty if qty > 0 else 0.0

    @property
    def unrealized_pnl(self) -> float:
        return self.quantity_on_hand * self.current_price - self.total_cost

    @property
    def total_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl

    @property
    def current_value(self) -> float:
        return self.quantity_on_hand * self.current_price

    def summary(self) -> Dict[str, Optional[float]]:
        return {
            "asset": self.asset,
            "asset_type": self.asset_type,
            "quantity": self.quantity_on_hand,
            "current_price": self.current_price,
            "average_cost": self.average_cost,
            "break_even": self.average_cost,
            "current_value": self.current_value,
            "realized_pnl": self.realized_pnl,
            "unrealized_pnl": self.unrealized_pnl,
            "total_pnl": self.total_pnl,
        }

class Portfolio:
    def __init__(self) -> None:
        self.positions: Dict[str, AssetPosition] = {}
        self._lock = threading.Lock()

    def _key(self, asset: str, asset_type: str) -> str:
        return f"{asset.upper()}|{asset_type.lower()}"

    def add_transaction(self, transaction: Transaction) -> AssetPosition:
        with self._lock:
            key = self._key(transaction.asset, transaction.asset_type)
            if key not in self.positions:
                self.positions[key] = AssetPosition(asset=transaction.asset.upper(), asset_type=transaction.asset_type)
            position = self.positions[key]
            position.add_transaction(transaction)
            return position

    def all_positions(self) -> List[AssetPosition]:
        with self._lock:
            return list(self.positions.values())

    def get_position(self, asset: str, asset_type: str) -> AssetPosition:
        with self._lock:
            key = self._key(asset, asset_type)
            return self.positions[key]

    def total_portfolio_value(self) -> float:
        with self._lock:
            return sum(position.current_value for position in self.positions.values())

    def total_pnl(self) -> float:
        with self._lock:
            return sum(position.total_pnl for position in self.positions.values())
