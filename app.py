import json
import os
import threading
import time
import queue
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure
import mplfinance as mpf

from api_client import PriceApiClient
from portfolio_manager import Portfolio, Transaction
from telegram_alert import TelegramNotifier
from database import DatabaseManager

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import config as user_config
except ImportError:
    user_config = None

POLL_INTERVAL = getattr(user_config, "PRICE_POLL_INTERVAL_SECONDS", 10)
TELEGRAM_TOKEN = getattr(user_config, "TELEGRAM_BOT_TOKEN", None) or os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = getattr(user_config, "TELEGRAM_CHAT_ID", None) or os.getenv("TELEGRAM_CHAT_ID")
BINANCE_API_KEY = getattr(user_config, "BINANCE_API_KEY", "") or os.getenv("BINANCE_API_KEY", "")
BINANCE_API_SECRET = getattr(user_config, "BINANCE_API_SECRET", "") or os.getenv("BINANCE_API_SECRET", "")
SQL_SERVER = getattr(user_config, "SQL_SERVER", "") or os.getenv("SQL_SERVER", "localhost")
SQL_DATABASE = getattr(user_config, "SQL_DATABASE", "") or os.getenv("SQL_DATABASE", "PortfolioDB")
SQL_TRUSTED_CONNECTION = getattr(user_config, "SQL_TRUSTED_CONNECTION", True)

DATA_FILE = Path(__file__).parent / "portfolio_data.json"
THEME_FILE = Path(__file__).parent / ".theme"

THEMES = {
    "dark": {
        "bg": "#1e1e2e", "bg_light": "#2a2a3c", "bg_frame": "#252536",
        "fg": "#cdd6f4", "fg_dim": "#6c7086", "accent": "#89b4fa",
        "green": "#a6e3a1", "red": "#f38ba8", "yellow": "#f9e2af",
        "btn": "#45475a", "btn_hover": "#585b70", "entry": "#313244",
        "accent_dark": "#1e1e2e", "tree_bg": "#2a2a3c", "tree_sel": "#89b4fa",
        "tree_sel_fg": "#1e1e2e", "header": "#89b4fa", "scrollbar": "#45475a",
        "trough": "#1e1e2e", "green_btn": "#2d5a3d", "green_btn_hover": "#3a6b4a",
        "red_btn": "#5a2d3d", "red_btn_hover": "#6b3a4a",
    },
    "light": {
        "bg": "#eff1f5", "bg_light": "#e6e9ef", "bg_frame": "#dce0e8",
        "fg": "#4c4f69", "fg_dim": "#8c8fa1", "accent": "#1e66f5",
        "green": "#40a02b", "red": "#d20f39", "yellow": "#df8e1d",
        "btn": "#ccd0da", "btn_hover": "#bcc0cc", "entry": "#e6e9ef",
        "accent_dark": "#eff1f5", "tree_bg": "#e6e9ef", "tree_sel": "#1e66f5",
        "tree_sel_fg": "#eff1f5", "header": "#1e66f5", "scrollbar": "#ccd0da",
        "trough": "#eff1f5", "green_btn": "#d5f0cc", "green_btn_hover": "#c0e8b5",
        "red_btn": "#f5d0d8", "red_btn_hover": "#f0b8c4",
    },
}


class PortfolioApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("Quản lý Danh mục & Cảnh báo Giá")
        self.root.geometry("1280x800")
        self.root.minsize(1050, 650)
        self.portfolio = Portfolio()
        self.api_client = PriceApiClient(api_key=BINANCE_API_KEY, api_secret=BINANCE_API_SECRET)
        self.telegram = TelegramNotifier(TELEGRAM_TOKEN, TELEGRAM_CHAT_ID)
        self.update_queue: queue.Queue = queue.Queue()
        self.log_queue: queue.Queue = queue.Queue()
        self.running = False
        self.alert_state: dict = {}
        self.price_alerts: list = []
        self._lock = threading.Lock()
        self.theme_name = self._load_theme_pref()
        self.t = THEMES[self.theme_name]
        self.use_db = False
        self.db: Optional[DatabaseManager] = None

        self._build_menu()
        self._build_ui()
        self._apply_theme()

        if SQL_SERVER:
            self.db = DatabaseManager(SQL_SERVER, SQL_DATABASE, SQL_TRUSTED_CONNECTION)
            threading.Thread(target=self._init_db, daemon=True).start()
        else:
            self._load_data()
        self.root.after(1000, self._process_queue)
        self.root.after(200, self._process_log_queue)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _init_db(self) -> None:
        try:
            if self.db.setup_database():
                self.use_db = True
                self.root.after(0, self._load_data)
            else:
                self.root.after(0, self._load_from_json)
                self._queue_log("Không kết nối được SQL Server, dùng file JSON.")
        except Exception as exc:
            self.root.after(0, self._load_from_json)
            self._queue_log(f"Lỗi kết nối cơ sở dữ liệu: {exc}")

    def _load_theme_pref(self) -> str:
        if THEME_FILE.exists():
            try:
                return THEME_FILE.read_text().strip()
            except Exception:
                pass
        return "dark"

    def _save_theme_pref(self) -> None:
        try:
            THEME_FILE.write_text(self.theme_name)
        except Exception:
            pass

    # ── Menu ───────────────────────────────────────────────────────────

    def _build_menu(self) -> None:
        self.menubar = tk.Menu(self.root, borderwidth=0)
        self.root.config(menu=self.menubar)

        self.file_menu = tk.Menu(self.menubar, tearoff=0)
        self.file_menu.add_command(label="Lưu dữ liệu", command=self._save_data, accelerator="Ctrl+S")
        self.file_menu.add_command(label="Tải lại dữ liệu", command=self._load_data)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="Thoát", command=self._on_close, accelerator="Alt+F4")
        self.menubar.add_cascade(label="Tệp", menu=self.file_menu)

        self.tools_menu = tk.Menu(self.menubar, tearoff=0)
        self.tools_menu.add_command(label="Làm mới giá ngay", command=self.on_refresh_now)
        self.tools_menu.add_command(label="Kiểm tra Telegram", command=self.on_test_telegram)
        self.tools_menu.add_separator()
        self.tools_menu.add_command(label="Xem số dư Binance", command=self.on_binance_balance)
        self.tools_menu.add_command(label="Lịch sử giao dịch Binance", command=self.on_binance_trades)
        self.tools_menu.add_command(label="Lệnh đang mở Binance", command=self.on_binance_orders)
        self.menubar.add_cascade(label="Công cụ", menu=self.tools_menu)

        self.help_menu = tk.Menu(self.menubar, tearoff=0)
        self.help_menu.add_command(label="Hướng dẫn sử dụng", command=self._show_help)
        self.help_menu.add_command(label="Giới thiệu", command=self._show_about)
        self.menubar.add_cascade(label="Trợ giúp", menu=self.help_menu)

        self.root.bind("<Control-s>", lambda e: self._save_data())

    # ── Giao diện chính ────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.main = ttk.Frame(self.root, padding=8)
        self.main.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)

        # Thanh tiêu đề màu accent
        self.header = tk.Frame(self.main, height=4)
        self.header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.header.grid_propagate(False)

        # Dòng tiêu đề + nút đổi theme
        self.title_frame = tk.Frame(self.main)
        self.title_frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        self.title_label = tk.Label(self.title_frame, text="QUẢN LÝ DANH MỤC ĐẦU TƯ", font=("Segoe UI", 18, "bold"))
        self.title_label.pack(side="left")
        self.subtitle_label = tk.Label(self.title_frame, text="  Cảnh báo giá Tiền điện tử & Cổ phiếu", font=("Segoe UI", 11))
        self.subtitle_label.pack(side="left", padx=(8, 0), pady=(6, 0))

        self.theme_btn = tk.Button(self.title_frame, text="Giao diện sáng", font=("Segoe UI", 9), relief="flat", cursor="hand2", command=self.toggle_theme)
        self.theme_btn.pack(side="right", padx=8)

        # PanedWindow: bảng trên + form dưới
        paned = ttk.PanedWindow(self.main, orient="vertical")
        paned.grid(row=2, column=0, sticky="nsew", pady=(0, 6))
        self.main.rowconfigure(2, weight=1)
        self.main.columnconfigure(0, weight=1)

        # === Bảng danh mục (phía trên) ===
        table_frame = ttk.Frame(paned)
        paned.add(table_frame, weight=3)

        cols = ("asset", "type", "qty", "price", "avg", "value", "realized", "unrealized", "total")
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings", height=8)
        headings = [
            ("asset", "Tài sản", 80), ("type", "Loại", 70), ("qty", "Số lượng", 100),
            ("price", "Giá hiện tại", 110), ("avg", "Giá vốn", 100), ("value", "Giá trị", 120),
            ("realized", "Lãi/Lỗ thực", 110), ("unrealized", "Lãi/Lỗ chờ", 110),
            ("total", "Tổng Lãi/Lỗ", 110),
        ]
        for key, label, width in headings:
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="center")

        self.scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=self.scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        # === Phần dưới: trái (form + nút) + phải (nhật ký) ===
        bottom = ttk.Frame(paned)
        paned.add(bottom, weight=2)

        left_panel = ttk.Frame(bottom)
        left_panel.pack(side="left", fill="both", expand=True, padx=(0, 6))

        # --- Form giao dịch ---
        self.form_frame = ttk.LabelFrame(left_panel, text="Giao dịch", padding=10)
        self.form_frame.pack(fill="x", pady=(0, 6))

        ttk.Label(self.form_frame, text="Mã tài sản:").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        self.asset_entry = ttk.Entry(self.form_frame, width=14, font=("Segoe UI", 10))
        self.asset_entry.grid(row=0, column=1, sticky="w", padx=4, pady=4)
        ttk.Label(self.form_frame, text="Loại:").grid(row=0, column=2, sticky="w", padx=(12, 4), pady=4)
        self.asset_type = tk.StringVar(value="crypto")
        self.type_combo = ttk.Combobox(self.form_frame, textvariable=self.asset_type, values=["Tiền điện tử", "Cổ phiếu"], width=12, state="readonly")
        self.type_combo.grid(row=0, column=3, sticky="w", padx=4, pady=4)
        self.type_combo.set("Tiền điện tử")

        ttk.Label(self.form_frame, text="Hình thức:").grid(row=1, column=0, sticky="w", padx=4, pady=4)
        self.side = tk.StringVar(value="buy")
        self.side_combo = ttk.Combobox(self.form_frame, textvariable=self.side, values=["Mua", "Bán"], width=12, state="readonly")
        self.side_combo.grid(row=1, column=1, sticky="w", padx=4, pady=4)
        self.side_combo.set("Mua")
        ttk.Label(self.form_frame, text="Số lượng:").grid(row=1, column=2, sticky="w", padx=(12, 4), pady=4)
        self.quantity_entry = ttk.Entry(self.form_frame, width=14, font=("Segoe UI", 10))
        self.quantity_entry.grid(row=1, column=3, sticky="w", padx=4, pady=4)

        ttk.Label(self.form_frame, text="Giá giao dịch:").grid(row=2, column=0, sticky="w", padx=4, pady=4)
        self.price_entry = ttk.Entry(self.form_frame, width=14, font=("Segoe UI", 10))
        self.price_entry.grid(row=2, column=1, sticky="w", padx=4, pady=4)

        ttk.Label(self.form_frame, text="Giá mục tiêu:").grid(row=3, column=0, sticky="w", padx=4, pady=4)
        self.target_price_entry = ttk.Entry(self.form_frame, width=14, font=("Segoe UI", 10))
        self.target_price_entry.grid(row=3, column=1, sticky="w", padx=4, pady=4)
        ttk.Label(self.form_frame, text="Hướng:").grid(row=3, column=2, sticky="w", padx=(12, 4), pady=4)
        self.alert_direction = tk.StringVar(value="Chốt lời")
        ttk.Combobox(self.form_frame, textvariable=self.alert_direction, values=["Chốt lời", "Cắt lỗ"], width=12, state="readonly").grid(row=3, column=3, sticky="w", padx=4, pady=4)

        # --- Hàng nút thao tác ---
        btn_frame = ttk.Frame(left_panel)
        btn_frame.pack(fill="x", pady=(0, 6))

        row1 = ttk.Frame(btn_frame)
        row1.pack(fill="x", pady=2)
        self.btn_add = ttk.Button(row1, text="➕ Thêm giao dịch", style="Accent.TButton", command=self.on_add_transaction)
        self.btn_add.pack(side="left", padx=3)
        self.btn_threshold = ttk.Button(row1, text="🔔 Đặt cảnh báo giá", command=self.on_set_thresholds)
        self.btn_threshold.pack(side="left", padx=3)
        self.btn_delete = ttk.Button(row1, text="🗑 Xoá vị thế", style="Red.TButton", command=self.on_delete_position)
        self.btn_delete.pack(side="left", padx=3)
        self.btn_clear_form = ttk.Button(row1, text="🧹 Xoá trắng", command=self._clear_form)
        self.btn_clear_form.pack(side="left", padx=3)

        row2 = ttk.Frame(btn_frame)
        row2.pack(fill="x", pady=2)
        self.btn_start = ttk.Button(row2, text="▶ Bắt đầu cập nhật giá", style="Green.TButton", command=self.start_updates)
        self.btn_start.pack(side="left", padx=3)
        self.btn_stop = ttk.Button(row2, text="⏹ Dừng cập nhật", style="Red.TButton", command=self.stop_updates)
        self.btn_stop.pack(side="left", padx=3)
        ttk.Button(row2, text="🔄 Làm mới giá ngay", command=self.on_refresh_now).pack(side="left", padx=3)
        ttk.Button(row2, text="📨 Kiểm tra Telegram", command=self.on_test_telegram).pack(side="left", padx=3)

        # --- Khung Binance ---
        self.binance_frame = ttk.LabelFrame(left_panel, text="Binance API", padding=8)
        self.binance_frame.pack(fill="x", pady=(0, 6))
        bin_row = ttk.Frame(self.binance_frame)
        bin_row.pack(fill="x")
        ttk.Button(bin_row, text="💰 Số dư", command=self.on_binance_balance).pack(side="left", padx=3)
        ttk.Button(bin_row, text="📋 Lịch sử GD", command=self.on_binance_trades).pack(side="left", padx=3)
        ttk.Button(bin_row, text="📑 Lệnh đang mở", command=self.on_binance_orders).pack(side="left", padx=3)
        ttk.Button(bin_row, text="🗄 Lịch sử DB", command=self.on_view_history).pack(side="left", padx=3)
        ttk.Button(bin_row, text="📊 Biểu đồ", command=self.on_show_chart).pack(side="left", padx=3)
        binance_status = "Đã kết nối" if BINANCE_API_KEY else "Chưa cấu hình API key"
        self.binance_status_label = tk.Label(bin_row, text=f"|  {binance_status}", font=("Segoe UI", 9))
        self.binance_status_label.pack(side="left", padx=8)

        # --- Nhật ký (bên phải) ---
        self.right_panel = ttk.LabelFrame(bottom, text="Nhật ký hoạt động", padding=6)
        self.right_panel.pack(side="right", fill="both", expand=True)
        self.log_text = tk.Text(self.right_panel, state="disabled", wrap="word", font=("Consolas", 10), borderwidth=0)
        self.log_text.pack(fill="both", expand=True)

        # === Thanh trạng thái (dưới cùng) ===
        self.status_frame = tk.Frame(self.root, height=30)
        self.status_frame.grid(row=1, column=0, sticky="ew")
        self.status_frame.grid_propagate(False)

        tg_status = "Telegram: Đã kết nối" if self.telegram.is_configured() else "Telegram: Chưa cấu hình"
        self.tg_label = tk.Label(self.status_frame, text=tg_status, font=("Segoe UI", 9))
        self.tg_label.pack(side="left", padx=12, pady=4)

        self.pnl_label = tk.Label(self.status_frame, text="Tổng Lãi/Lỗ: 0.00", font=("Segoe UI", 9, "bold"))
        self.pnl_label.pack(side="left", padx=12, pady=4)

        self.update_status_label = tk.Label(self.status_frame, text="Cập nhật giá: Tắt", font=("Segoe UI", 9))
        self.update_status_label.pack(side="left", padx=12, pady=4)

        self.time_label = tk.Label(self.status_frame, text="", font=("Segoe UI", 9))
        self.time_label.pack(side="right", padx=12, pady=4)
        self._update_clock()

    # ── Theme ──────────────────────────────────────────────────────────

    def toggle_theme(self) -> None:
        self.theme_name = "light" if self.theme_name == "dark" else "dark"
        self.t = THEMES[self.theme_name]
        self._apply_theme()
        self._save_theme_pref()

    def _apply_theme(self) -> None:
        t = self.t
        style = ttk.Style()
        style.theme_use("clam")

        style.configure(".", background=t["bg"], foreground=t["fg"], fieldbackground=t["entry"], borderwidth=0)
        style.configure("TFrame", background=t["bg"])
        style.configure("TLabel", background=t["bg"], foreground=t["fg"], font=("Segoe UI", 10))
        style.configure("TLabelframe", background=t["bg_frame"], foreground=t["accent"], font=("Segoe UI", 10, "bold"))
        style.configure("TLabelframe.Label", background=t["bg_frame"], foreground=t["accent"], font=("Segoe UI", 10, "bold"))
        style.configure("TButton", background=t["btn"], foreground=t["fg"], font=("Segoe UI", 10), padding=(12, 6), borderwidth=0)
        style.map("TButton", background=[("active", t["btn_hover"]), ("pressed", t["accent"])])
        style.configure("Accent.TButton", background=t["accent"], foreground=t["accent_dark"], font=("Segoe UI", 10, "bold"), padding=(12, 6))
        style.map("Accent.TButton", background=[("active", t["btn_hover"]), ("pressed", t["accent"])])
        style.configure("Green.TButton", background=t["green_btn"], foreground=t["green"], font=("Segoe UI", 10, "bold"), padding=(12, 6))
        style.map("Green.TButton", background=[("active", t["green_btn_hover"])])
        style.configure("Red.TButton", background=t["red_btn"], foreground=t["red"], font=("Segoe UI", 10, "bold"), padding=(12, 6))
        style.map("Red.TButton", background=[("active", t["red_btn_hover"])])
        style.configure("TCombobox", fieldbackground=t["entry"], background=t["btn"], foreground=t["fg"], arrowcolor=t["fg"])
        style.map("TCombobox", fieldbackground=[("readonly", t["entry"])], foreground=[("readonly", t["fg"])])
        style.configure("TEntry", fieldbackground=t["entry"], foreground=t["fg"], insertcolor=t["fg"])
        style.configure("Treeview", background=t["tree_bg"], foreground=t["fg"], fieldbackground=t["tree_bg"], rowheight=28, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", background=t["btn"], foreground=t["accent"], font=("Segoe UI", 10, "bold"), borderwidth=0)
        style.map("Treeview", background=[("selected", t["tree_sel"])], foreground=[("selected", t["tree_sel_fg"])])
        style.map("Treeview.Heading", background=[("active", t["btn_hover"])])
        style.configure("Vertical.TScrollbar", background=t["scrollbar"], troughcolor=t["trough"], borderwidth=0, arrowcolor=t["fg"])

        self.root.configure(bg=t["bg"])
        self.main.configure(style="TFrame")
        self.header.configure(bg=t["header"])
        self.title_frame.configure(bg=t["bg"])
        self.title_label.configure(bg=t["bg"], fg=t["accent"])
        self.subtitle_label.configure(bg=t["bg"], fg=t["fg_dim"])
        self.theme_btn.configure(bg=t["btn"], fg=t["fg"], activebackground=t["btn_hover"], activeforeground=t["fg"])

        self.binance_status_label.configure(bg=t["bg_frame"], fg=t["green"] if BINANCE_API_KEY else t["yellow"])

        self.log_text.configure(bg=t["bg_light"], fg=t["fg"], insertbackground=t["fg"], selectbackground=t["accent"], selectforeground=t["accent_dark"])

        self.status_frame.configure(bg=t["bg_frame"])
        tg_ok = self.telegram.is_configured()
        self.tg_label.configure(bg=t["bg_frame"], fg=t["green"] if tg_ok else t["yellow"])
        self.pnl_label.configure(bg=t["bg_frame"], fg=t["fg"])
        self.update_status_label.configure(bg=t["bg_frame"], fg=t["fg_dim"])
        self.time_label.configure(bg=t["bg_frame"], fg=t["fg_dim"])

        self.tree.tag_configure("profit", foreground=t["green"])
        self.tree.tag_configure("loss", foreground=t["red"])
        self.tree.tag_configure("even", foreground=t["fg"])

        menus = [self.menubar, self.file_menu, self.tools_menu, self.help_menu]
        for m in menus:
            m.configure(bg=t["bg_frame"], fg=t["fg"], activebackground=t["accent"], activeforeground=t["accent_dark"])

        self.theme_btn.configure(text="Giao diện sáng" if self.theme_name == "dark" else "Giao diện tối")

    def _update_clock(self) -> None:
        self.time_label.configure(text=datetime.now().strftime("%H:%M:%S"))
        self.root.after(1000, self._update_clock)

    # ── Chuyển đổi giá trị Combobox ───────────────────────────────────

    def _get_asset_type(self) -> str:
        """Chuyển giá trị combobox loại tài sản sang giá trị kỹ thuật."""
        val = self.type_combo.get()
        return "crypto" if val == "Tiền điện tử" else "stock"

    def _get_side(self) -> str:
        """Chuyển giá trị combobox hình thức sang giá trị kỹ thuật."""
        val = self.side_combo.get()
        return "buy" if val == "Mua" else "sell"

    # ── Xử lý sự kiện ─────────────────────────────────────────────────

    def _clear_form(self) -> None:
        """Xoá trắng tất cả trường nhập liệu."""
        self.asset_entry.delete(0, "end")
        self.quantity_entry.delete(0, "end")
        self.price_entry.delete(0, "end")
        self.target_price_entry.delete(0, "end")
        self.type_combo.set("Tiền điện tử")
        self.side_combo.set("Mua")
        self.alert_direction.set("Chốt lời")

    def on_add_transaction(self) -> None:
        asset = self.asset_entry.get().strip().upper()
        asset_type = self._get_asset_type()
        side = self._get_side()
        try:
            quantity = float(self.quantity_entry.get())
            price = float(self.price_entry.get())
        except ValueError:
            messagebox.showerror("Lỗi dữ liệu", "Vui lòng nhập số lượng và giá hợp lệ.")
            return
        if not asset:
            messagebox.showerror("Lỗi dữ liệu", "Vui lòng nhập mã tài sản.")
            return
        if quantity <= 0:
            messagebox.showerror("Lỗi dữ liệu", "Số lượng phải lớn hơn 0.")
            return
        if price <= 0:
            messagebox.showerror("Lỗi dữ liệu", "Giá giao dịch phải lớn hơn 0.")
            return
        transaction = Transaction(asset=asset, asset_type=asset_type, quantity=quantity, price=price, side=side, date=datetime.now())
        try:
            self.portfolio.add_transaction(transaction)
        except ValueError as err:
            messagebox.showerror("Lỗi giao dịch", str(err))
            return
        side_label = "MUA" if side == "buy" else "BÁN"
        self.log(f"Đã thêm {side_label} {asset} {quantity} @ {price}")
        self.refresh_table()
        if self.use_db and self.db:
            self.db.save_transaction(asset, asset_type, side, quantity, price)
        self._save_data()

    def on_set_thresholds(self) -> None:
        asset = self.asset_entry.get().strip().upper()
        if not asset:
            messagebox.showerror("Lỗi dữ liệu", "Vui lòng nhập mã tài sản để đặt cảnh báo.")
            return
        target_price = self._parse_optional_float(self.target_price_entry.get())
        if target_price is None or target_price <= 0:
            messagebox.showerror("Lỗi dữ liệu", "Vui lòng nhập giá mục tiêu hợp lệ (lớn hơn 0).")
            return
        direction_raw = self.alert_direction.get()
        direction = "above" if direction_raw == "Chốt lời" else "below"
        if self.use_db and self.db:
            self.db.save_price_alert(asset, target_price, direction)
        self.log(f"Đã đặt cảnh báo cho {asset}: {direction_raw} @ {target_price}")
        self._load_price_alerts()

    def on_delete_position(self) -> None:
        asset = self.asset_entry.get().strip().upper()
        asset_type = self._get_asset_type()
        if not asset:
            messagebox.showerror("Lỗi dữ liệu", "Vui lòng nhập mã tài sản để xoá.")
            return
        key = self.portfolio._key(asset, asset_type)
        if key not in self.portfolio.positions:
            messagebox.showerror("Lỗi", f"Không tìm thấy vị thế {asset} ({asset_type}).")
            return
        if not messagebox.askyesno("Xác nhận xoá", f"Bạn có chắc muốn xoá toàn bộ vị thế {asset} ({asset_type})?"):
            return
        with self.portfolio._lock:
            del self.portfolio.positions[key]
        self.log(f"Đã xoá vị thế {asset} ({asset_type})")
        self.refresh_table()
        if self.use_db and self.db:
            self.db.delete_portfolio(asset, asset_type)
            self.db.delete_price_alerts_by_symbol(asset)
        self._save_data()

    def on_view_history(self) -> None:
        if not self.use_db or not self.db or not self.db.is_connected():
            messagebox.showwarning("Cơ sở dữ liệu", "Chưa kết nối SQL Server. Cần cấu hình trong config.py")
            return
        asset = self.asset_entry.get().strip().upper()
        history = self.db.get_transaction_history(asset if asset else None, limit=50)
        if not history:
            self.log("Không có lịch sử giao dịch trong cơ sở dữ liệu.")
            return
        win = tk.Toplevel(self.root)
        win.title("Lịch sử giao dịch (SQL Server)")
        win.geometry("750x400")
        win.configure(bg=self.t["bg"])
        cols = ("id", "asset", "type", "side", "qty", "price", "pnl", "date")
        tree = ttk.Treeview(win, columns=cols, show="headings", height=15)
        for key, label, w in [("id", "ID", 50), ("asset", "Tài sản", 80), ("type", "Loại", 70), ("side", "Hình thức", 80), ("qty", "Số lượng", 100), ("price", "Giá", 100), ("pnl", "Lãi/Lỗ", 100), ("date", "Thời gian", 150)]:
            tree.heading(key, text=label)
            tree.column(key, width=w, anchor="center")
        tree.pack(fill="both", expand=True, padx=8, pady=8)
        for tx in history:
            side_label = "MUA" if tx["side"] == "buy" else "BÁN"
            date_str = tx["created_at"].strftime("%Y-%m-%d %H:%M:%S") if tx["created_at"] else "-"
            tree.insert("", "end", values=(tx["id"], tx["asset"], tx["asset_type"], side_label, f"{tx['quantity']:.6f}", f"{tx['price']:.2f}", f"{tx['realized_pnl']:+.2f}", date_str))

    def on_show_chart(self) -> None:
        asset = self.asset_entry.get().strip().upper()
        if not asset:
            messagebox.showwarning("Biểu đồ", "Nhập mã tài sản (ví dụ BTC) để xem biểu đồ.")
            return
        win = tk.Toplevel(self.root)
        win.title(f"Biểu đồ nến - {asset}USDT")
        win.geometry("900x600")
        win.configure(bg=self.t["bg"])

        ctrl_frame = tk.Frame(win, bg=self.t["bg"])
        ctrl_frame.pack(fill="x", padx=8, pady=4)
        tk.Label(ctrl_frame, text="Khung thời gian:", font=("Segoe UI", 10), bg=self.t["bg"], fg=self.t["fg"]).pack(side="left", padx=4)
        interval_var = tk.StringVar(value="1h")
        intervals = [("1 phút", "1m"), ("5 phút", "5m"), ("15 phút", "15m"), ("1 giờ", "1h"), ("4 giờ", "4h"), ("1 ngày", "1d")]
        for label, val in intervals:
            tk.Radiobutton(ctrl_frame, text=label, variable=interval_var, value=val, font=("Segoe UI", 9), bg=self.t["bg"], fg=self.t["fg"], selectcolor=self.t["btn"], activebackground=self.t["bg"], activeforeground=self.t["accent"]).pack(side="left", padx=2)

        chart_frame = tk.Frame(win, bg=self.t["bg"])
        chart_frame.pack(fill="both", expand=True, padx=8, pady=4)

        def render_chart():
            for widget in chart_frame.winfo_children():
                widget.destroy()
            interval = interval_var.get()
            try:
                df = self.api_client.get_klines(asset, interval=interval, limit=100)
            except Exception as exc:
                tk.Label(chart_frame, text=f"Lỗi: {exc}", font=("Segoe UI", 11), bg=self.t["bg"], fg=self.t["red"]).pack(pady=20)
                return

            mc = mpf.make_marketcolors(
                up=self.t["green"], down=self.t["red"],
                edge="inherit", wick="inherit", volume="in",
                ohlc="i"
            )
            s = mpf.make_mpf_style(
                marketcolors=mc,
                base_mpf_style="nightclouds",
                facecolor=self.t["bg_light"],
                edgecolor=self.t["fg_dim"],
                gridcolor=self.t["btn"],
                gridstyle="--",
                rc={"font.size": 9}
            )

            fig, axes = mpf.plot(
                df, type="candle", style=s, volume=True,
                title=f"\n{asset}USDT ({interval})",
                ylabel="Giá (USDT)", ylabel_lower="Khối lượng",
                figsize=(11, 6), returnfig=True,
                figscale=1.0,
            )
            axes[0].set_title(f"{asset}USDT ({interval})", color=self.t["accent"], fontsize=13, fontweight="bold", pad=10)

            canvas = FigureCanvasTkAgg(fig, master=chart_frame)
            canvas.draw()
            canvas.get_tk_widget().pack(fill="both", expand=True)
            plt.close(fig)

        tk.Button(ctrl_frame, text="Vẽ biểu đồ", font=("Segoe UI", 10, "bold"), bg=self.t["accent"], fg=self.t["accent_dark"], relief="flat", cursor="hand2", command=render_chart).pack(side="left", padx=12)
        render_chart()

    def on_refresh_now(self) -> None:
        positions = self.portfolio.all_positions()
        if not positions:
            self.log("Không có vị thế nào để cập nhật.")
            return
        self.log("Đang làm mới giá...")
        for position in positions:
            try:
                price = self.api_client.get_price(position.asset, position.asset_type)
                position.current_price = price
                self.log(f"  {position.asset}: {price:.2f}")
            except Exception as exc:
                self.log(f"  Lỗi cập nhật {position.asset}: {exc}")
        self.refresh_table()

    def on_test_telegram(self) -> None:
        if not self.telegram.is_configured():
            messagebox.showwarning("Telegram", "Telegram chưa cấu hình. Kiểm tra config.py hoặc .env")
            return
        try:
            self.telegram.send_message("Kiểm tra kết nối từ ứng dụng Quản lý Danh mục - Thành công!")
            self.log("Đã gửi tin nhắn kiểm tra Telegram.")
            messagebox.showinfo("Telegram", "Gửi thành công! Kiểm tra Telegram của bạn.")
        except Exception as exc:
            self.log(f"Lỗi kiểm tra Telegram: {exc}")
            messagebox.showerror("Telegram", f"Lỗi: {exc}")

    def on_binance_balance(self) -> None:
        try:
            balances = self.api_client.get_account_balance()
        except ValueError as exc:
            messagebox.showwarning("Binance", str(exc))
            return
        except Exception as exc:
            messagebox.showerror("Binance", f"Lỗi: {exc}")
            return
        if not balances:
            self.log("Tài khoản Binance: không có tài sản.")
            return
        self.log("=== Số dư Binance ===")
        for b in balances:
            self.log(f"  {b['asset']}: {b['total']:.8f} (khả dụng: {b['free']:.8f}, khoá: {b['locked']:.8f})")
        self.log(f"Tổng {len(balances)} tài sản có số dư.")

    def on_binance_trades(self) -> None:
        asset = self.asset_entry.get().strip().upper()
        if not asset:
            messagebox.showwarning("Binance", "Nhập mã tài sản (ví dụ BTC) để xem lịch sử.")
            return
        try:
            trades = self.api_client.get_my_trades(asset, limit=20)
        except ValueError as exc:
            messagebox.showwarning("Binance", str(exc))
            return
        except Exception as exc:
            messagebox.showerror("Binance", f"Lỗi: {exc}")
            return
        if not trades:
            self.log(f"Không có lịch sử giao dịch cho {asset}USDT.")
            return
        self.log(f"=== Lịch sử {asset}USDT (20 lệnh gần nhất) ===")
        for t in trades:
            side = "MUA" if t["is_buyer"] else "BÁN"
            ts = datetime.fromtimestamp(t["time"] / 1000).strftime("%Y-%m-%d %H:%M:%S")
            self.log(f"  [{ts}] {side} {t['qty']:.8f} @ {t['price']:.2f} (phí: {t['commission']} {t['commission_asset']})")

    def on_binance_orders(self) -> None:
        asset = self.asset_entry.get().strip().upper()
        try:
            orders = self.api_client.get_open_orders(asset if asset else None)
        except ValueError as exc:
            messagebox.showwarning("Binance", str(exc))
            return
        except Exception as exc:
            messagebox.showerror("Binance", f"Lỗi: {exc}")
            return
        if not orders:
            self.log("Không có lệnh đang mở trên Binance.")
            return
        self.log(f"=== Lệnh đang mở ({len(orders)}) ===")
        for o in orders:
            ts = datetime.fromtimestamp(o["time"] / 1000).strftime("%Y-%m-%d %H:%M:%S")
            self.log(f"  [{ts}] {o['symbol']} {o['side']} {o['type']} {o['qty']:.8f} @ {o['price']:.2f} ({o['status']})")

    def _parse_optional_float(self, text: str) -> Optional[float]:
        text = text.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    # ── Bảng danh mục ──────────────────────────────────────────────────

    def refresh_table(self) -> None:
        for row in self.tree.get_children():
            self.tree.delete(row)
        for position in self.portfolio.all_positions():
            total_pnl = position.total_pnl
            tag = "profit" if total_pnl > 0 else ("loss" if total_pnl < 0 else "even")
            type_label = "Crypto" if position.asset_type == "crypto" else "CP"
            self.tree.insert("", "end", iid=f"{position.asset}_{position.asset_type}", tags=(tag,), values=(
                position.asset, type_label,
                f"{position.quantity_on_hand:.6f}", f"{position.current_price:.2f}",
                f"{position.average_cost:.2f}", f"{position.current_value:.2f}",
                f"{position.realized_pnl:+.2f}", f"{position.unrealized_pnl:+.2f}",
                f"{position.total_pnl:+.2f}",
            ))
        total = self.portfolio.total_pnl()
        pnl_color = self.t["green"] if total > 0 else (self.t["red"] if total < 0 else self.t["fg"])
        self.pnl_label.configure(text=f"Tổng Lãi/Lỗ: {total:+.2f} USD", fg=pnl_color)

    # ── Cập nhật giá nền ───────────────────────────────────────────────

    def start_updates(self) -> None:
        if self.running:
            return
        self.running = True
        self.update_status_label.configure(text="Cập nhật giá: Bật", fg=self.t["green"])
        thread = threading.Thread(target=self._price_update_loop, daemon=True)
        thread.start()
        self.log("Bắt đầu cập nhật giá nền...")

    def stop_updates(self) -> None:
        self.running = False
        self.update_status_label.configure(text="Cập nhật giá: Tắt", fg=self.t["fg_dim"])
        self.log("Đã dừng cập nhật giá.")

    def _price_update_loop(self) -> None:
        while self.running:
            positions = self.portfolio.all_positions()
            for position in positions:
                if not self.running:
                    break
                try:
                    price = self.api_client.get_price(position.asset, position.asset_type)
                    position.current_price = price
                    self.update_queue.put(position.asset)
                    self._check_alert(position)
                except Exception as exc:
                    self._queue_log(f"Lỗi cập nhật giá {position.asset}: {exc}")
            time.sleep(POLL_INTERVAL)

    # ── Cảnh báo giá ───────────────────────────────────────────────────

    def _check_alert(self, position) -> None:
        current = position.current_price
        symbol = position.asset.upper()
        with self._lock:
            for alert in self.price_alerts:
                if alert["symbol"] != symbol or not alert["is_active"]:
                    continue
                triggered = False
                if alert["direction"] == "below" and current <= alert["target_price"]:
                    triggered = True
                elif alert["direction"] == "above" and current >= alert["target_price"]:
                    triggered = True
                if triggered:
                    alert_key = (alert["id"],)
                    if self.alert_state.get(alert_key) != "triggered":
                        self._send_price_alert(position, alert)
                        self.alert_state[alert_key] = "triggered"
                        if self.use_db and self.db:
                            self.db.mark_alert_notified(alert["id"])

    def _send_price_alert(self, position, alert: dict) -> None:
        direction_label = "Chốt lời" if alert["direction"] == "above" else "Cắt lỗ"
        message = (
            f"[{direction_label}] {position.asset} ({position.asset_type})\n"
            f"Giá hiện tại: {position.current_price:.2f}\n"
            f"Mục tiêu: {alert['target_price']:.2f}\n"
            f"Số lượng: {position.quantity_on_hand:.6f}\n"
            f"Lãi/Lỗ chờ: {position.unrealized_pnl:.2f}\n"
            f"Lãi/Lỗ thực: {position.realized_pnl:.2f}"
        )
        if self.telegram.is_configured():
            self.telegram.send_message(message)
            self._queue_log(f"Đã gửi cảnh báo Telegram cho {position.asset}: {direction_label} @ {alert['target_price']}")
        else:
            self._queue_log(f"Cảnh báo cho {position.asset}: {direction_label} @ {alert['target_price']} (Telegram chưa cấu hình)")

    # ── Nhật ký & Hàng đợi ─────────────────────────────────────────────

    def _queue_log(self, message: str) -> None:
        self.log_queue.put(message)

    def _process_log_queue(self) -> None:
        while not self.log_queue.empty():
            try:
                msg = self.log_queue.get_nowait()
                self.log(msg)
            except queue.Empty:
                break
        self.root.after(200, self._process_log_queue)

    def _process_queue(self) -> None:
        updated = False
        while not self.update_queue.empty():
            try:
                self.update_queue.get_nowait()
                updated = True
            except queue.Empty:
                break
        if updated:
            self.refresh_table()
        self.root.after(1000, self._process_queue)

    def log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{timestamp}] {message}\n")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    # ── Lưu / Tải dữ liệu ─────────────────────────────────────────────

    def _save_data(self) -> None:
        if self.use_db and self.db and self.db.is_connected():
            return
        data = {"transactions": []}
        for key, pos in self.portfolio.positions.items():
            for lot in pos.lots:
                data["transactions"].append({"asset": pos.asset, "asset_type": pos.asset_type, "side": "buy", "quantity": lot.quantity, "price": lot.price})
            data["transactions"].append({"asset": pos.asset, "asset_type": pos.asset_type, "realized_pnl": pos.realized_pnl, "_realized_only": True})
        try:
            with open(DATA_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            self.log(f"Lỗi lưu dữ liệu: {exc}")

    def _load_data(self) -> None:
        if self.use_db and self.db and self.db.is_connected():
            self._load_from_db()
            return
        self._load_from_json()

    def _load_price_alerts(self) -> None:
        if self.use_db and self.db and self.db.is_connected():
            self.price_alerts = self.db.load_price_alerts()

    def _load_from_db(self) -> None:
        transactions = self.db.load_transactions()
        for tx in transactions:
            transaction = Transaction(
                asset=tx["asset"], asset_type=tx["asset_type"],
                quantity=tx["quantity"], price=tx["price"],
                side=tx["side"], date=datetime.now()
            )
            self.portfolio.add_transaction(transaction)
            if tx["realized_pnl"] != 0:
                key = self.portfolio._key(tx["asset"], tx["asset_type"])
                if key in self.portfolio.positions:
                    self.portfolio.positions[key].realized_pnl += tx["realized_pnl"]
        self.price_alerts = self.db.load_price_alerts()
        self.refresh_table()
        self.log(f"Đã tải dữ liệu từ SQL Server ({len(transactions)} giao dịch, {len(self.price_alerts)} cảnh báo)")

    def _load_from_json(self) -> None:
        if not DATA_FILE.exists():
            return
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            self.log(f"Lỗi đọc dữ liệu: {exc}")
            return
        for tx in data.get("transactions", []):
            if tx.get("_realized_only"):
                key = self.portfolio._key(tx["asset"], tx["asset_type"])
                if key in self.portfolio.positions:
                    self.portfolio.positions[key].realized_pnl = tx.get("realized_pnl", 0.0)
                continue
            transaction = Transaction(asset=tx["asset"], asset_type=tx["asset_type"], quantity=tx["quantity"], price=tx["price"], side=tx["side"], date=datetime.now())
            self.portfolio.add_transaction(transaction)
        self.refresh_table()
        self.log(f"Đã tải dữ liệu từ {DATA_FILE.name}")

    # ── Đóng ứng dụng ──────────────────────────────────────────────────

    def _on_close(self) -> None:
        self.running = False
        self._save_data()
        if self.db:
            self.db.close()
        self.root.destroy()

    # ── Trợ giúp & Giới thiệu ─────────────────────────────────────────

    def _show_help(self) -> None:
        help_win = tk.Toplevel(self.root)
        help_win.title("Hướng dẫn sử dụng")
        help_win.geometry("620x550")
        help_win.configure(bg=self.t["bg"])
        text = tk.Text(help_win, wrap="word", bg=self.t["bg_light"], fg=self.t["fg"], font=("Consolas", 11), padx=16, pady=16, borderwidth=0)
        text.pack(fill="both", expand=True)
        text.insert("1.0", """HƯỚNG DẪN SỬ DỤNG

1. THÊM GIAO DỊCH
   - Nhập mã tài sản (ví dụ: BTC, ETH, AAPL)
   - Chọn loại: Tiền điện tử hoặc Cổ phiếu
   - Chọn hình thức: Mua hoặc Bán
   - Nhập số lượng và giá giao dịch
   - Nhấn "Thêm giao dịch"

2. ĐẶT CẢNH BÁO GIÁ
   - Nhập mã tài sản đã thêm
   - Nhập Giá mục tiêu
   - Chọn Hướng: Chốt lời (cảnh báo khi giá vượt trên)
     hoặc Cắt lỗ (cảnh báo khi giá rớt dưới)
   - Nhấn "Đặt cảnh báo giá"
   - Khi giá chạm mục tiêu, Telegram sẽ tự động cảnh báo

3. CẬP NHẬT GIÁ
   - Nhấn "Bắt đầu cập nhật giá" để tự động lấy giá
   - Nhấn "Làm mới giá ngay" để cập nhật ngay lập tức
   - Nhấn "Dừng cập nhật" để tắt

4. BINANCE API
   - "Số dư": Xem tất cả tài sản trên Binance
   - "Lịch sử GD": Xem 20 lệnh gần nhất của một tài sản
   - "Lệnh đang mở": Xem các lệnh chờ trên Binance

5. TELEGRAM
   - "Kiểm tra Telegram": Gửi tin nhắn kiểm tra kết nối
   - Tự động cảnh báo khi giá chạm mục tiêu

6. GIAO DIỆN SÁNG/TỐI
   - Nhấn nút góc phải trên để chuyển đổi giao diện

7. LƯU DỮ LIỆU
   - Dữ liệu tự động lưu khi thêm giao dịch
   - Nhấn Ctrl+S để lưu thủ công""")
        text.configure(state="disabled")

    def _show_about(self) -> None:
        messagebox.showinfo("Giới thiệu",
            "Quản lý Danh mục & Cảnh báo Giá\n\n"
            "Hệ thống quản lý danh mục đầu tư\n"
            "và cảnh báo giá tiền điện tử / cổ phiếu\n\n"
            "Tích hợp:\n"
            "- Binance API (giá crypto theo thời gian thực)\n"
            "- Yahoo Finance (giá cổ phiếu)\n"
            "- Telegram Bot API (cảnh báo tự động)\n\n"
            "Đa luồng cập nhật giá không làm đơ giao diện\n"
            "Tính lãi/lỗ theo phương pháp FIFO")


if __name__ == "__main__":
    root = tk.Tk()
    app = PortfolioApp(root)
    root.mainloop()
