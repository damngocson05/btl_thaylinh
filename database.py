from __future__ import annotations
import pyodbc
from datetime import datetime
from typing import List, Dict, Optional


class DatabaseManager:
    def __init__(self, server: str, database: str, trusted_connection: bool = True) -> None:
        self.server = server
        self.database = database
        self.trusted_connection = trusted_connection
        self.conn: Optional[pyodbc.Connection] = None

    def connect(self) -> bool:
        try:
            if self.trusted_connection:
                conn_str = (
                    f"DRIVER={{ODBC Driver 17 for SQL Server}};"
                    f"SERVER={self.server};"
                    f"DATABASE={self.database};"
                    f"Trusted_Connection=yes;"
                )
            else:
                conn_str = (
                    f"DRIVER={{ODBC Driver 17 for SQL Server}};"
                    f"SERVER={self.server};"
                    f"DATABASE={self.database};"
                )
            self.conn = pyodbc.connect(conn_str, timeout=10)
            return True
        except Exception:
            return False

    def connect_master(self) -> Optional[pyodbc.Connection]:
        try:
            if self.trusted_connection:
                conn_str = (
                    f"DRIVER={{ODBC Driver 17 for SQL Server}};"
                    f"SERVER={self.server};"
                    f"DATABASE=master;"
                    f"Trusted_Connection=yes;"
                )
            else:
                conn_str = (
                    f"DRIVER={{ODBC Driver 17 for SQL Server}};"
                    f"SERVER={self.server};"
                    f"DATABASE=master;"
                )
            return pyodbc.connect(conn_str, timeout=10)
        except Exception:
            return None

    def setup_database(self) -> bool:
        master = self.connect_master()
        if not master:
            return False
        try:
            master.autocommit = True
            cursor = master.cursor()
            cursor.execute(f"""
                IF NOT EXISTS (SELECT name FROM sys.databases WHERE name = N'{self.database}')
                CREATE DATABASE [{self.database}]
            """)
            cursor.close()
            master.close()
        except Exception:
            return False

        if not self.connect():
            return False

        try:
            cursor = self.conn.cursor()

            # Drop old tables if they exist (old schema without FK)
            cursor.execute("""
                IF EXISTS (SELECT * FROM sysobjects WHERE name='alerts' AND xtype='U')
                DROP TABLE alerts
            """)
            cursor.execute("""
                IF EXISTS (SELECT * FROM sysobjects WHERE name='transactions' AND xtype='U')
                DROP TABLE transactions
            """)

            # Create portfolios table (parent)
            cursor.execute("""
                IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='portfolios' AND xtype='U')
                CREATE TABLE portfolios (
                    id INT IDENTITY(1,1) PRIMARY KEY,
                    symbol NVARCHAR(20) NOT NULL,
                    asset_type NVARCHAR(20) NOT NULL,
                    created_at DATETIME DEFAULT GETDATE(),
                    UNIQUE(symbol, asset_type)
                )
            """)

            # Create transactions table (child, FK to portfolios)
            cursor.execute("""
                IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='transactions' AND xtype='U')
                CREATE TABLE transactions (
                    id INT IDENTITY(1,1) PRIMARY KEY,
                    portfolio_id INT NOT NULL,
                    symbol NVARCHAR(20) NOT NULL,
                    side NVARCHAR(10) NOT NULL,
                    quantity FLOAT NOT NULL,
                    price FLOAT NOT NULL,
                    realized_pnl FLOAT DEFAULT 0.0,
                    created_at DATETIME DEFAULT GETDATE(),
                    FOREIGN KEY (portfolio_id) REFERENCES portfolios(id) ON DELETE CASCADE
                )
            """)

            # Create price_alerts table (independent)
            cursor.execute("""
                IF NOT EXISTS (SELECT * FROM sysobjects WHERE name='price_alerts' AND xtype='U')
                CREATE TABLE price_alerts (
                    id INT IDENTITY(1,1) PRIMARY KEY,
                    symbol NVARCHAR(20) NOT NULL,
                    target_price FLOAT NOT NULL,
                    direction NVARCHAR(10) NOT NULL,
                    is_active BIT DEFAULT 1,
                    created_at DATETIME DEFAULT GETDATE(),
                    notified_at DATETIME NULL
                )
            """)

            self.conn.commit()
            cursor.close()
            return True
        except Exception:
            return False

    def is_connected(self) -> bool:
        if not self.conn:
            return False
        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT 1")
            cursor.close()
            return True
        except Exception:
            return False

    # ── Portfolio helpers ──────────────────────────────────────────────

    def get_or_create_portfolio(self, symbol: str, asset_type: str) -> Optional[int]:
        """Find existing portfolio or create new one. Returns portfolio_id."""
        if not self.is_connected():
            return None
        try:
            cursor = self.conn.cursor()
            symbol = symbol.upper()
            cursor.execute(
                "SELECT id FROM portfolios WHERE symbol = ? AND asset_type = ?",
                (symbol, asset_type)
            )
            row = cursor.fetchone()
            if row:
                cursor.close()
                return row[0]
            cursor.execute(
                "INSERT INTO portfolios (symbol, asset_type) OUTPUT INSERTED.id VALUES (?, ?)",
                (symbol, asset_type)
            )
            portfolio_id = cursor.fetchone()[0]
            self.conn.commit()
            cursor.close()
            return portfolio_id
        except Exception:
            return None

    def delete_portfolio(self, symbol: str, asset_type: str) -> bool:
        """Delete portfolio and CASCADE delete all its transactions."""
        if not self.is_connected():
            return False
        try:
            cursor = self.conn.cursor()
            cursor.execute(
                "DELETE FROM portfolios WHERE symbol = ? AND asset_type = ?",
                (symbol.upper(), asset_type)
            )
            self.conn.commit()
            cursor.close()
            return True
        except Exception:
            return False

    # ── Transactions ───────────────────────────────────────────────────

    def save_transaction(self, symbol: str, asset_type: str, side: str, quantity: float, price: float, realized_pnl: float = 0.0) -> bool:
        if not self.is_connected():
            return False
        try:
            portfolio_id = self.get_or_create_portfolio(symbol, asset_type)
            if portfolio_id is None:
                return False
            cursor = self.conn.cursor()
            cursor.execute(
                "INSERT INTO transactions (portfolio_id, symbol, side, quantity, price, realized_pnl) VALUES (?, ?, ?, ?, ?, ?)",
                (portfolio_id, symbol.upper(), side, quantity, price, realized_pnl)
            )
            self.conn.commit()
            cursor.close()
            return True
        except Exception:
            return False

    def load_transactions(self) -> List[Dict]:
        if not self.is_connected():
            return []
        try:
            cursor = self.conn.cursor()
            cursor.execute("""
                SELECT t.symbol, p.asset_type, t.side, t.quantity, t.price, t.realized_pnl, t.created_at
                FROM transactions t
                JOIN portfolios p ON t.portfolio_id = p.id
                ORDER BY t.id
            """)
            rows = cursor.fetchall()
            cursor.close()
            transactions = []
            for row in rows:
                transactions.append({
                    "asset": row[0],
                    "asset_type": row[1],
                    "side": row[2],
                    "quantity": row[3],
                    "price": row[4],
                    "realized_pnl": row[5],
                    "created_at": row[6],
                })
            return transactions
        except Exception:
            return []

    def get_transaction_history(self, symbol: Optional[str] = None, limit: int = 100) -> List[Dict]:
        if not self.is_connected():
            return []
        try:
            cursor = self.conn.cursor()
            if symbol:
                cursor.execute("""
                    SELECT TOP (?) t.id, t.symbol, p.asset_type, t.side, t.quantity, t.price, t.realized_pnl, t.created_at
                    FROM transactions t
                    JOIN portfolios p ON t.portfolio_id = p.id
                    WHERE t.symbol = ?
                    ORDER BY t.id DESC
                """, (limit, symbol.upper()))
            else:
                cursor.execute("""
                    SELECT TOP (?) t.id, t.symbol, p.asset_type, t.side, t.quantity, t.price, t.realized_pnl, t.created_at
                    FROM transactions t
                    JOIN portfolios p ON t.portfolio_id = p.id
                    ORDER BY t.id DESC
                """, (limit,))
            rows = cursor.fetchall()
            cursor.close()
            history = []
            for row in rows:
                history.append({
                    "id": row[0],
                    "asset": row[1],
                    "asset_type": row[2],
                    "side": row[3],
                    "quantity": row[4],
                    "price": row[5],
                    "realized_pnl": row[6],
                    "created_at": row[7],
                })
            return history
        except Exception:
            return []

    # ── Price Alerts ───────────────────────────────────────────────────

    def save_price_alert(self, symbol: str, target_price: float, direction: str) -> bool:
        """Create or update a price alert. direction: 'above' or 'below'."""
        if not self.is_connected():
            return False
        try:
            cursor = self.conn.cursor()
            symbol = symbol.upper()
            cursor.execute("""
                IF EXISTS (SELECT 1 FROM price_alerts WHERE symbol = ? AND direction = ?)
                    UPDATE price_alerts SET target_price = ?, is_active = 1, notified_at = NULL, created_at = GETDATE() WHERE symbol = ? AND direction = ?
                ELSE
                    INSERT INTO price_alerts (symbol, target_price, direction) VALUES (?, ?, ?)
            """, (symbol, direction, target_price, symbol, direction, symbol, target_price, direction))
            self.conn.commit()
            cursor.close()
            return True
        except Exception:
            return False

    def load_price_alerts(self) -> List[Dict]:
        if not self.is_connected():
            return []
        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT id, symbol, target_price, direction, is_active, created_at, notified_at FROM price_alerts WHERE is_active = 1")
            rows = cursor.fetchall()
            cursor.close()
            alerts = []
            for row in rows:
                alerts.append({
                    "id": row[0],
                    "symbol": row[1],
                    "target_price": row[2],
                    "direction": row[3],
                    "is_active": row[4],
                    "created_at": row[5],
                    "notified_at": row[6],
                })
            return alerts
        except Exception:
            return []

    def mark_alert_notified(self, alert_id: int) -> bool:
        if not self.is_connected():
            return False
        try:
            cursor = self.conn.cursor()
            cursor.execute("UPDATE price_alerts SET notified_at = GETDATE() WHERE id = ?", (alert_id,))
            self.conn.commit()
            cursor.close()
            return True
        except Exception:
            return False

    def delete_price_alert(self, alert_id: int) -> bool:
        if not self.is_connected():
            return False
        try:
            cursor = self.conn.cursor()
            cursor.execute("DELETE FROM price_alerts WHERE id = ?", (alert_id,))
            self.conn.commit()
            cursor.close()
            return True
        except Exception:
            return False

    def delete_price_alerts_by_symbol(self, symbol: str) -> bool:
        if not self.is_connected():
            return False
        try:
            cursor = self.conn.cursor()
            cursor.execute("DELETE FROM price_alerts WHERE symbol = ?", (symbol.upper(),))
            self.conn.commit()
            cursor.close()
            return True
        except Exception:
            return False

    def close(self) -> None:
        if self.conn:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None
