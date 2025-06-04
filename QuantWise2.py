from functools import lru_cache
import threading
from dotenv import load_dotenv
from scipy.stats import norm
from arch import arch_model
import yfinance as yf
import time
from plotly.subplots import make_subplots
import streamlit as st
import requests
import pandas as pd
import numpy as np
import plotly.graph_objs as go
import os
import cvxpy as cp
from datetime import datetime, timedelta
import sqlite3
import bcrypt
import json
import websockets
import asyncio
import nest_asyncio
from binance.client import Client
from binance import AsyncClient, BinanceSocketManager
import smtplib
from email.mime.text import MIMEText
import secrets
import re
import plotly.express as px
from wordcloud import WordCloud
from binance.exceptions import BinanceAPIException
# Apply nest_asyncio for async in Streamlit
nest_asyncio.apply()

# Get environment variables (will be set in Streamlit Cloud)
API_KEY = os.environ.get("TWELVE_DATA_API_KEY", "010b5dc49d784e548e99c2f6da02a266")
BINANCE_API_KEY = os.environ.get("BINANCE_API_KEY")
BINANCE_SECRET_KEY = os.environ.get("BINANCE_SECRET_KEY")
binance_client = None
BINANCE_TLD = 'us'  # Default to Binance US
EMAIL_USER = os.environ.get("EMAIL_USER", "your_email@example.com")
EMAIL_PASSWORD = os.environ.get("EMAIL_PASSWORD", "your_email_password")

# Initialize Binance client
if BINANCE_API_KEY and BINANCE_SECRET_KEY:
    try:
        # First try global Binance (non-US)
        binance_client = Client(
            api_key=BINANCE_API_KEY.strip(),
            api_secret=BINANCE_SECRET_KEY.strip(),
            tld='com'  # Use global endpoint
        )
        binance_client.get_symbol_info('BTCUSDT')  # Test API
        st.toast("Connected to Binance Global API", icon="🌎")
    except BinanceAPIException as e:
        if "restricted location" in str(e).lower():
            try:
                # Fall back to Binance US
                binance_client = Client(
                    api_key=BINANCE_API_KEY.strip(),
                    api_secret=BINANCE_SECRET_KEY.strip(),
                    tld='us'  # US-specific endpoint
                )
                binance_client.ping()
                st.toast("Connected to Binance US API", icon="🇺🇸")
            except BinanceAPIException as e2:
                st.error(f"Binance US API Error: {e2.message}")
                binance_client = None
        else:
            st.error(f"Binance Global API Error: {e.message}")
            binance_client = None
    except Exception as e:
        st.error(f"Error initializing Binance: {str(e)}")
        binance_client = None
# Database connection pooling
db_lock = threading.Lock()

def get_db_connection():
    db_dir = os.path.join(os.path.expanduser("~"), ".quantwise")
    os.makedirs(db_dir, exist_ok=True)
    db_path = os.path.join(db_dir, "users.db")
    return sqlite3.connect(db_path, check_same_thread=False)


# ---------------- SETUP DATABASE ----------------
def create_subscriptions_table():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS subscriptions (
                username TEXT PRIMARY KEY,
                tier TEXT NOT NULL,
                gpu_hours INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(username) REFERENCES users(username)
            )''')
    conn.commit()
    conn.close()

def init_subscription(username, tier='free'):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO subscriptions (username, tier, gpu_hours) VALUES (?, ?, ?)",
             (username, tier, 10 if tier == 'free' else 1000))
    conn.commit()
    conn.close()

def check_usage_limit(username):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT tier, gpu_hours FROM subscriptions WHERE username = ?", (username,))
    sub = c.fetchone()
    conn.close()
    return sub or ('free', 10)

def update_usage(username, hours_used):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE subscriptions SET gpu_hours = gpu_hours - ? WHERE username = ?", 
             (hours_used, username))
    conn.commit()
    conn.close()

def log_event(username, event_type, details):
    with db_lock:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    details TEXT NOT NULL,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )''')
        c.execute("INSERT INTO audit_log (username, event_type, details) VALUES (?, ?, ?)",
                 (username, event_type, json.dumps(details)))
        conn.commit()
        conn.close()

def init_db():
     with db_lock:
        conn = get_db_connection()
        c = conn.cursor() 
        c.execute('''CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password TEXT NOT NULL,
                email TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''') 
        # Create other tables
        # c.execute
        c.execute('''CREATE TABLE IF NOT EXISTS portfolios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                name TEXT NOT NULL,
                data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(username) REFERENCES users(username)
            )''') 
        c.execute('''CREATE TABLE IF NOT EXISTS simulations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                portfolio_name TEXT NOT NULL,
                simulation_data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(username) REFERENCES users(username)
            )''')
        create_subscriptions_table()
        # Create feedback table
        c.execute('''CREATE TABLE IF NOT EXISTS user_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                rating INTEGER NOT NULL,
                feedback TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )''')
        conn.commit()
        conn.close()
    
if "db_initialized" not in st.session_state:
    init_db()
    st.session_state.db_initialized = True
    
def add_user(username, password, email):
    hashed_pw = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    conn = get_db_connection()
    c = conn.cursor()
    try:
        c.execute("INSERT INTO users (username, password, email) VALUES (?, ?, ?)", 
                 (username, hashed_pw, email))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def get_user(username):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE username = ?", (username,))
    result = c.fetchone()
    conn.close()
    return result

def verify_password(input_pw, stored_hashed_pw):
    return bcrypt.checkpw(input_pw.encode(), stored_hashed_pw.encode())

def save_portfolio(username, name, portfolio_data):
    conn = get_db_connection()
    c = conn.cursor()
    json_data = json.dumps(portfolio_data)
    c.execute("INSERT INTO portfolios (username, name, data) VALUES (?, ?, ?)", (username, name, json_data))
    conn.commit()
    conn.close()

def load_portfolios(username):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, name, data, created_at FROM portfolios WHERE username = ? ORDER BY created_at DESC", (username,))
    rows = c.fetchall()
    conn.close()
    portfolios = []
    for row in rows:
        portfolios.append({
            "id": row[0],
            "name": row[1],
            "data": json.loads(row[2]),
            "created_at": row[3]
        })
    return portfolios

def save_simulation(username, portfolio_name, simulation_array):
    conn = get_db_connection()
    c = conn.cursor()
    simulation_list = simulation_array.tolist()
    json_data = json.dumps(simulation_list)
    c.execute("INSERT INTO simulations (username, portfolio_name, simulation_data) VALUES (?, ?, ?)",
              (username, portfolio_name, json_data))
    conn.commit()
    conn.close()

def load_simulations(username):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT id, portfolio_name, simulation_data, created_at FROM simulations WHERE username = ? ORDER BY created_at DESC", (username,))
    rows = c.fetchall()
    conn.close()
    simulations = []
    for row in rows:
        simulations.append({
            "id": row[0],
            "portfolio_name": row[1],
            "simulation_data": np.array(json.loads(row[2])),
            "created_at": row[3]
        })
    return simulations

def save_feedback(username, rating, feedback):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO user_feedback (username, rating, feedback) VALUES (?, ?, ?)",
              (username, rating, feedback))
    conn.commit()
    conn.close()


# ---------------- EMAIL VERIFICATION ----------------

# ---------------- SESSION STATE ----------------
if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False
if "username" not in st.session_state:
    st.session_state["username"] = ""
if "mode" not in st.session_state:
    st.session_state["mode"] = "login"
if "df" not in st.session_state:
    st.session_state.df = None
if "simulations" not in st.session_state:
    st.session_state.simulations = None
if "real_time_active" not in st.session_state:
    st.session_state.real_time_active = False
if "real_time_data" not in st.session_state:
    st.session_state.real_time_data = pd.DataFrame()
if "crypto_symbols" not in st.session_state:
    st.session_state.crypto_symbols = []

init_db()



# ---------------- SIGNUP ----------------
BETA_CODES = ["QUANTBETA1", "QUANTBETA2", "QUANTBETA3"]

def signup():
    # ...
    beta_code = st.text_input("Beta Access Code")
    if beta_code not in BETA_CODES:
        st.error("Invalid beta access code")
        return
    st.title("📝 Create Account")
    new_user = st.text_input("Username")
    new_email = st.text_input("Email")
    new_pass = st.text_input("Password", type="password")
    confirm_pass = st.text_input("Confirm Password", type="password")
    
    if st.button("Create Account"):
        # Validation checks
        if not new_user:
            st.error("Please enter a username.")
            return
        if not re.match(r"[^@]+@[^@]+\.[^@]+", new_email):
            st.error("Please enter a valid email address.")
            return
        if new_pass != confirm_pass:
            st.error("Passwords do not match.")
            return
        if len(new_pass) < 6:
            st.error("Password must be at least 6 characters.")
            return
            
        # Check if username exists
        if get_user(new_user):
            st.error("Username already exists.")
            return
            
        # Create user
        if add_user(new_user, new_pass, new_email):
            st.success("Account created successfully! You can now log in.")
            log_event(new_user, "signup", {"email": new_email})
            st.session_state["mode"] = "login"
            st.rerun()
        else:
            st.error("Failed to create account. Please try again.")
# ---------------- LOGIN ----------------
def login():
    st.title("🔐 Login")
    user = st.text_input("Username")
    pw = st.text_input("Password", type="password")
    
    if st.button("Login"):
        account = get_user(user)
        if account and verify_password(pw, account[1]):
            st.session_state["authenticated"] = True
            st.session_state["username"] = user
            init_subscription(user)
            log_event(user, "login", {})
            st.rerun()
        else:
            st.error("Invalid username or password.")
# ---------------- BINANCE FUNCTIONS ----------------
def get_binance_data(symbol, interval, days):
    try:
        # Check if client is initialized
        if not binance_client:
            st.error("⚠️ Binance API credentials not configured")
            log_event(st.session_state["username"], "binance_error", {"error": "Client not initialized"})
            return None

        # Map interval to Binance format
        interval_map = {
            "1min": Client.KLINE_INTERVAL_1MINUTE,
            "5min": Client.KLINE_INTERVAL_5MINUTE,
            "15min": Client.KLINE_INTERVAL_15MINUTE,
            "30min": Client.KLINE_INTERVAL_30MINUTE,
            "1h": Client.KLINE_INTERVAL_1HOUR,
            "1day": Client.KLINE_INTERVAL_1DAY
        }
        
        if interval not in interval_map:
            st.warning(f"Unsupported interval: {interval}. Using 1 day as default")
            binance_interval = Client.KLINE_INTERVAL_1DAY
        else:
            binance_interval = interval_map[interval]
        
        # Calculate start and end times
        end_time = datetime.now()
        start_time = end_time - timedelta(days=days)
        
        # Fetch klines with retry logic
        max_retries = 3
        for attempt in range(max_retries):
            try:
                klines = binance_client.get_historical_klines(
                    symbol=symbol,
                    interval=binance_interval,
                    start_str=start_time.strftime("%d %b %Y %H:%M:%S"),
                    end_str=end_time.strftime("%d %b %Y %H:%M:%S")
                )
                break  # Exit loop if successful
            except BinanceAPIException as api_ex:
                if api_ex.code == -1003:  # Rate limit exceeded
                    wait_time = 2 ** attempt  # Exponential backoff
                    st.warning(f"Rate limit exceeded. Retrying in {wait_time} seconds...")
                    time.sleep(wait_time)
                else:
                    raise
            except (ConnectionError, TimeoutError):
                st.warning(f"Network error. Retry {attempt+1}/{max_retries}")
                time.sleep(1)
        else:
            st.error("Failed to fetch data after multiple attempts")
            return None

        # Validate response
        if not klines:
            st.warning(f"No data returned for {symbol}. Check symbol format.")
            return None
            
        # Create DataFrame
        columns = [
            'timestamp', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_asset_volume', 'num_trades',
            'taker_buy_base', 'taker_buy_quote', 'ignore'
        ]
        df = pd.DataFrame(klines, columns=columns)
        
        # Convert columns to numeric
        numeric_cols = ['open', 'high', 'low', 'close', 'volume']
        df[numeric_cols] = df[numeric_cols].apply(pd.to_numeric, errors='coerce')
        
        # Convert timestamp to datetime
        df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('datetime', inplace=True)
        
        # Filter and return
        return df[['open', 'high', 'low', 'close', 'volume']]
        
    except BinanceAPIException as e:
        # Handle specific Binance errors
        error_msg = f"Binance API error ({e.status_code}): "
        if e.code == -1121:
            error_msg += f"Invalid symbol: {symbol}"
        elif e.code == -1100:
            error_msg += "Illegal characters in parameter"
        elif e.code == -1021:
            error_msg += "Timestamp error - check system clock"
        elif e.code == -1003:
            error_msg += "Rate limit exceeded - try again later"
        else:
            error_msg += f"Code {e.code}: {e.message}"
            
        st.error(error_msg)
        log_event(st.session_state["username"], "binance_api_error", {
            "symbol": symbol,
            "code": e.code,
            "message": e.message
        })
        return None
        
    except Exception as e:
        # Handle all other exceptions
        st.error(f"Unexpected error fetching Binance data: {str(e)}")
        log_event(st.session_state["username"], "binance_generic_error", {
            "symbol": symbol,
            "error": str(e)
        })
        return None
# ---------------- REAL-TIME DATA STREAMING ----------------
async def binance_real_time(symbol, interval):
    try:
        # Determine which TLD to use based on our synchronous client
        tld = 'us' if hasattr(binance_client, 'tld') and binance_client.tld == 'us' else 'com'
        
        # Create async client with proper TLD
        client = await AsyncClient.create(
            api_key=BINANCE_API_KEY.strip(),
            api_secret=BINANCE_SECRET_KEY.strip(),
            tld=tld
        )
        
        bm = BinanceSocketManager(client)
        
        # Map interval to Binance format
        interval_map = {
            "1min": Client.KLINE_INTERVAL_1MINUTE,
            "5min": Client.KLINE_INTERVAL_5MINUTE,
            "15min": Client.KLINE_INTERVAL_15MINUTE,
            "30min": Client.KLINE_INTERVAL_30MINUTE,
            "1h": Client.KLINE_INTERVAL_1HOUR,
            "1day": Client.KLINE_INTERVAL_1DAY
        }
        binance_interval = interval_map.get(interval, Client.KLINE_INTERVAL_1MINUTE)
        
        # Start kline socket
        ts = bm.kline_socket(symbol, interval=binance_interval)
        
        # Then start receiving messages
        async with ts as tscm:
            while st.session_state.real_time_active:
                res = await tscm.recv()
                if 'k' in res:
                    kline = res['k']
                    new_data = pd.DataFrame([{
                        'datetime': pd.to_datetime(kline['t'], unit='ms'),
                        'open': float(kline['o']),
                        'high': float(kline['h']),
                        'low': float(kline['l']),
                        'close': float(kline['c']),
                        'volume': float(kline['v'])
                    }]).set_index('datetime')
                    
                    # Update session state
                    if st.session_state.real_time_data.empty:
                        st.session_state.real_time_data = new_data
                    else:
                        # Only add if it's new data
                        if new_data.index[0] > st.session_state.real_time_data.index.max():
                            st.session_state.real_time_data = pd.concat([
                                st.session_state.real_time_data, 
                                new_data
                            ])
                    
                    # Throttle updates to prevent UI overload
                    await asyncio.sleep(0.2)
                else:
                    st.warning(f"Unexpected real-time data format: {res}")
    
    except websockets.exceptions.ConnectionClosedError:
        st.error("Real-time connection closed unexpectedly")
    except asyncio.TimeoutError:
        st.error("Real-time connection timed out")
    except BinanceAPIException as e:
        if "restricted location" in str(e).lower():
            st.error("Binance API blocked: This location is restricted. Try Binance US instead.")
        else:
            st.error(f"Binance API error: {e.message}")
    except Exception as e:
        st.error(f"Unexpected error in real-time stream: {str(e)}")
    finally:
        try:
            await client.close_connection()
        except:
            pass
def start_real_time(symbol, interval, is_crypto):
    try:
        # Reset real-time data
        st.session_state.real_time_active = True
        st.session_state.real_time_data = pd.DataFrame()
        
        if is_crypto:
            # Determine TLD based on binance_client configuration
            tld = 'us'
            if binance_client and hasattr(binance_client, 'tld'):
                tld = binance_client.tld
            elif binance_client and 'us' in str(binance_client).lower():
                tld = 'us'
            else:
                tld = 'com'
                
            # Start the async task for crypto
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(binance_real_time(symbol, interval, tld))
        else:
            # For stocks, use a thread to periodically fetch data
            def stock_poller():
                yf_interval_map = {
                    "1min": "1m", "5min": "5m", "15min": "15m", 
                    "30min": "30m", "1h": "60m"
                }
                yf_interval = yf_interval_map.get(interval, "1m")
                
                while st.session_state.real_time_active:
                    try:
                        # Get data for the current day
                        data = yf.download(symbol, period='1d', interval=yf_interval, progress=False)
                        if not data.empty:
                            # Get the last data point
                            latest = data.iloc[-1]
                            
                            # Create new row with proper datetime index
                            new_row = pd.DataFrame({
                                'open': [latest['Open']],
                                'high': [latest['High']],
                                'low': [latest['Low']],
                                'close': [latest['Close']],
                                'volume': [latest['Volume']]
                            }, index=[data.index[-1]])
                            
                            # Append only if it's new data
                            if st.session_state.real_time_data.empty:
                                st.session_state.real_time_data = new_row
                            else:
                                last_index = st.session_state.real_time_data.index.max()
                                if pd.to_datetime(new_row.index[0]) > last_index:
                                    st.session_state.real_time_data = pd.concat([
                                        st.session_state.real_time_data, 
                                        new_row
                                    ])
                    except Exception as e:
                        st.error(f"Stock data error: {str(e)}")
                    
                    # Update every 10 seconds
                    time.sleep(10)
            
            # Start the thread
            threading.Thread(target=stock_poller, daemon=True).start()
            
    except Exception as e:
        st.error(f"Failed to start real-time: {str(e)}")
        st.session_state.real_time_active = False

# ---------------- ASSET DATA FETCHER ----------------
@lru_cache(maxsize=32)
def cached_asset_data(symbol, interval, days, is_crypto):
    return get_asset_data(symbol, interval, days, is_crypto)

def get_asset_data(symbol, interval, days, is_crypto, retries=3):
    if is_crypto:
        # Only use Twelve Data for cryptocurrency
        for attempt in range(retries):
            try:
                # Convert symbol to Twelve Data format (BTC/USD instead of BTCUSDT)
                if "/" not in symbol:
                    # Auto-convert common formats
                    if symbol.endswith("USDT"):
                        converted_symbol = f"{symbol[:-4]}/USD"
                    elif symbol.endswith("USD"):
                        converted_symbol = f"{symbol[:-3]}/USD"
                    else:
                        converted_symbol = symbol + "/USD"
                else:
                    converted_symbol = symbol
                
                end_date = datetime.now()
                start_date = end_date - timedelta(days=days)
                url = f"https://api.twelvedata.com/time_series?symbol={converted_symbol}&interval={interval}&start_date={start_date.strftime('%Y-%m-%d')}&end_date={end_date.strftime('%Y-%m-%d')}&apikey={API_KEY}&outputsize=5000"
                
                response = requests.get(url)
                data = response.json()
                
                # Check for API errors
                if "code" in data and data["code"] != 200:
                    error_msg = data.get("message", "Unknown error")
                    st.warning(f"Twelve Data API error: {error_msg}")
                    continue
                
                if "values" in data:
                    df = pd.DataFrame(data["values"])
                    # Handle possible date column names
                    date_col = "datetime" if "datetime" in df.columns else "date"
                    df[date_col] = pd.to_datetime(df[date_col])
                    df.set_index(date_col, inplace=True)
                    df = df.astype(float).sort_index()
                    
                    # Standardize column names
                    column_map = {
                        "open": "open",
                        "high": "high",
                        "low": "low",
                        "close": "close",
                        "volume": "volume"
                    }
                    df = df.rename(columns=column_map)
                    
                    # Ensure we have required columns
                    for col in ["open", "high", "low", "close"]:
                        if col not in df.columns:
                            df[col] = df["close"]
                            
                    if "volume" not in df.columns:
                        df["volume"] = 0
                    
                    return df
                else:
                    st.warning(f"Twelve Data returned no values: {data}")
            except Exception as e:
                st.warning(f"Attempt {attempt+1} failed: {str(e)}")
                time.sleep(2)
        
        # If all attempts fail, use Yahoo Finance as fallback
        st.warning("Twelve Data failed, using Yahoo Finance as fallback")
        try:
            interval_map = {
                "1min": "1m", "5min": "5m", "15min": "15m", 
                "30min": "30m", "1h": "60m", "1day": "1d"
            }
            yf_interval = interval_map.get(interval, "1d")
            yf_data = yf.download(symbol, period=f"{days+5}d", interval=yf_interval)
            
            if not yf_data.empty:
                # Create required columns
                required_columns = ['Open', 'High', 'Low', 'Close', 'Volume']
                for col in required_columns:
                    if col not in yf_data.columns:
                        yf_data[col] = yf_data['Close'] if 'Close' in yf_data.columns else 100
                        
                # Rename columns to lowercase
                yf_data = yf_data.rename(columns={
                    'Open': 'open',
                    'High': 'high',
                    'Low': 'low',
                    'Close': 'close',
                    'Volume': 'volume'
                })
                return yf_data[['open', 'high', 'low', 'close', 'volume']]
        except Exception as e:
            st.error(f"Yahoo Finance fallback failed: {str(e)}")
        
        return None
    else:
        # Stock data handling
        for attempt in range(retries):
            try:
                end_date = datetime.now()
                start_date = end_date - timedelta(days=days)
                url = f"https://api.twelvedata.com/time_series?symbol={symbol}&interval={interval}&start_date={start_date.strftime('%Y-%m-%d')}&end_date={end_date.strftime('%Y-%m-%d')}&apikey={API_KEY}&outputsize=5000"
                response = requests.get(url)
                data = response.json()
                
                if "values" in data:
                    df = pd.DataFrame(data["values"])
                    df["datetime"] = pd.to_datetime(df["datetime"])
                    df.set_index("datetime", inplace=True)
                    df = df.astype(float).sort_index()
                    return df
                else:
                    st.warning(f"Attempt {attempt+1} failed: {data.get('message', 'Unknown error')}")
                    time.sleep(2)
            except Exception as e:
                st.warning(f"Attempt {attempt+1} failed: {str(e)}")
                time.sleep(2)
        
        # Fallback to Yahoo Finance
        try:
            st.warning("Using Yahoo Finance as fallback data source")
            interval_map = {
                "1min": "1m", "5min": "5m", "15min": "15m", 
                "30min": "30m", "1h": "60m", "1day": "1d"
            }
            yf_interval = interval_map.get(interval, "1d")
            
            # Get more days to ensure enough data points
            yf_data = yf.download(symbol, period=f"{days+5}d", interval=yf_interval)
            
            if not yf_data.empty:
                # Create all required columns with default values if missing
                required_columns = ['Open', 'High', 'Low', 'Close', 'Volume']
                for col in required_columns:
                    if col not in yf_data.columns:
                        yf_data[col] = yf_data['Close'] if 'Close' in yf_data.columns else 100
                        
                # Rename columns to lowercase
                yf_data = yf_data.rename(columns={
                    'Open': 'open',
                    'High': 'high',
                    'Low': 'low',
                    'Close': 'close',
                    'Volume': 'volume'
                })
                return yf_data[['open', 'high', 'low', 'close', 'volume']]
        except Exception as e:
            st.error(f"Yahoo Finance fallback failed: {str(e)}")
        
        return None

# ---------------- TECHNICAL INDICATORS ----------------
def add_technical_indicators(df, sma_short, sma_long, rsi_period, bb_window=20, bb_std=2):
    # SMA with better handling of NaN values
    df["SMA_Short"] = df["close"].rolling(window=sma_short, min_periods=1).mean()
    df["SMA_Long"] = df["close"].rolling(window=sma_long, min_periods=1).mean()
    
    # Enhanced RSI calculation
    delta = df["close"].diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)
    avg_gain = gain.rolling(window=rsi_period, min_periods=1).mean()
    avg_loss = loss.rolling(window=rsi_period, min_periods=1).mean()
    rs = avg_gain / avg_loss.replace(0, 0.0001)  # Avoid division by zero
    df["RSI"] = 100 - (100 / (1 + rs))
    
    # Bollinger Bands
    df["BB_Mid"] = df["close"].rolling(window=bb_window).mean()
    df["BB_Std"] = df["close"].rolling(window=bb_window).std()
    df["BB_Upper"] = df["BB_Mid"] + (df["BB_Std"] * bb_std)
    df["BB_Lower"] = df["BB_Mid"] - (df["BB_Std"] * bb_std)
    
    # MACD
    exp12 = df["close"].ewm(span=12, adjust=False).mean()
    exp26 = df["close"].ewm(span=26, adjust=False).mean()
    df["MACD"] = exp12 - exp26
    df["Signal_Line"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df["MACD_Hist"] = df["MACD"] - df["Signal_Line"]
    
    # Stochastic Oscillator - only if we have high/low data
    if 'high' in df.columns and 'low' in df.columns:
        low_min = df["low"].rolling(window=14).min()
        high_max = df["high"].rolling(window=14).max()
        df["%K"] = 100 * ((df["close"] - low_min) / (high_max - low_min + 0.0001))  # Avoid division by zero
        df["%D"] = df["%K"].rolling(window=3).mean()
    else:
        st.warning("Skipping Stochastic Oscillator: Missing high/low data")
        df["%K"] = 50  # Default neutral value
        df["%D"] = 50
    
    return df

# ---------------- TRADING STRATEGIES ----------------
def backtest_sma(df, sma_short, sma_long, initial_capital=10000, slippage=0.001, commission=0):
    df = df.copy()
    # Calculate SMAs if not already present
    if "SMA_Short" not in df.columns:
        df["SMA_Short"] = df["close"].rolling(window=sma_short).mean()
    if "SMA_Long" not in df.columns:
        df["SMA_Long"] = df["close"].rolling(window=sma_long).mean()
    
    # Generate signals
    df["Signal"] = 0
    df.loc[df["SMA_Short"] > df["SMA_Long"], "Signal"] = 1  # Buy signal
    df.loc[df["SMA_Short"] < df["SMA_Long"], "Signal"] = -1  # Sell signal
    
    # Position management
    df["Position"] = df["Signal"].diff()
    df.loc[df["Position"] == 0, "Position"] = np.nan
    df["Position"].fillna(method='ffill', inplace=True)
    df["Position"].fillna(0, inplace=True)
    
    # Calculate returns with slippage and commission
    df["Strategy_Return"] = df["close"].pct_change() * df["Position"].shift(1)
    trade_indices = df["Position"] != df["Position"].shift(1)
    df.loc[trade_indices, "Strategy_Return"] -= slippage
    df.loc[trade_indices, "Strategy_Return"] -= commission / initial_capital
    
    # Cumulative performance
    df["Cumulative_Return"] = (1 + df["Strategy_Return"]).cumprod()
    df["Portfolio_Value"] = initial_capital * df["Cumulative_Return"]
    
    # Calculate metrics
    total_return = df["Cumulative_Return"].iloc[-1] - 1
    max_drawdown = (df["Cumulative_Return"] / df["Cumulative_Return"].cummax() - 1).min()
    
    return df, total_return, max_drawdown

def backtest_rsi(df, rsi_low, rsi_high, initial_capital=10000, slippage=0.001, commission=0):
    df = df.copy()
    # Calculate RSI if not already present
    if "RSI" not in df.columns:
        delta = df["close"].diff()
        gain = (delta.where(delta > 0, 0)).fillna(0)
        loss = (-delta.where(delta < 0, 0)).fillna(0)
        avg_gain = gain.rolling(window=14, min_periods=1).mean()
        avg_loss = loss.rolling(window=14, min_periods=1).mean()
        rs = avg_gain / avg_loss.replace(0, 0.0001)
        df["RSI"] = 100 - (100 / (1 + rs))
    
    # Generate signals
    df["Signal"] = 0
    df.loc[df["RSI"] < rsi_low, "Signal"] = 1  # Buy when RSI is low
    df.loc[df["RSI"] > rsi_high, "Signal"] = -1  # Sell when RSI is high
    
    # Position management
    df["Position"] = df["Signal"].diff()
    df.loc[df["Position"] == 0, "Position"] = np.nan
    df["Position"].fillna(method='ffill', inplace=True)
    df["Position"].fillna(0, inplace=True)
    
    # Calculate returns with slippage and commission
    df["Strategy_Return"] = df["close"].pct_change() * df["Position"].shift(1)
    trade_indices = df["Position"] != df["Position"].shift(1)
    df.loc[trade_indices, "Strategy_Return"] -= slippage
    df.loc[trade_indices, "Strategy_Return"] -= commission / initial_capital
    
    # Cumulative performance
    df["Cumulative_Return"] = (1 + df["Strategy_Return"]).cumprod()
    df["Portfolio_Value"] = initial_capital * df["Cumulative_Return"]
    
    # Calculate metrics
    total_return = df["Cumulative_Return"].iloc[-1] - 1
    max_drawdown = (df["Cumulative_Return"] / df["Cumulative_Return"].cummax() - 1).min()
    
    return df, total_return, max_drawdown

def backtest_bollinger_bands(df, bb_window, bb_std, initial_capital=10000, slippage=0.001, commission=0):
    df = df.copy()
    # Calculate Bollinger Bands if not already present
    if "BB_Lower" not in df.columns:
        df["BB_Mid"] = df["close"].rolling(window=bb_window).mean()
        df["BB_Std"] = df["close"].rolling(window=bb_window).std()
        df["BB_Upper"] = df["BB_Mid"] + (df["BB_Std"] * bb_std)
        df["BB_Lower"] = df["BB_Mid"] - (df["BB_Std"] * bb_std)
    
    # Generate signals
    df["Signal"] = 0
    df.loc[df["close"] < df["BB_Lower"], "Signal"] = 1  # Buy when price below lower band
    df.loc[df["close"] > df["BB_Upper"], "Signal"] = -1  # Sell when price above upper band
    
    # Position management
    df["Position"] = df["Signal"].diff()
    df.loc[df["Position"] == 0, "Position"] = np.nan
    df["Position"].fillna(method='ffill', inplace=True)
    df["Position"].fillna(0, inplace=True)
    
    # Calculate returns with slippage and commission
    df["Strategy_Return"] = df["close"].pct_change() * df["Position"].shift(1)
    trade_indices = df["Position"] != df["Position"].shift(1)
    df.loc[trade_indices, "Strategy_Return"] -= slippage
    df.loc[trade_indices, "Strategy_Return"] -= commission / initial_capital
    
    # Cumulative performance
    df["Cumulative_Return"] = (1 + df["Strategy_Return"]).cumprod()
    df["Portfolio_Value"] = initial_capital * df["Cumulative_Return"]
    
    # Calculate metrics
    total_return = df["Cumulative_Return"].iloc[-1] - 1
    max_drawdown = (df["Cumulative_Return"] / df["Cumulative_Return"].cummax() - 1).min()
    
    return df, total_return, max_drawdown

def backtest_macd(df, initial_capital=10000, slippage=0.001, commission=0):
    df = df.copy()
    # Calculate MACD if not already present
    if "MACD" not in df.columns:
        exp12 = df["close"].ewm(span=12, adjust=False).mean()
        exp26 = df["close"].ewm(span=26, adjust=False).mean()
        df["MACD"] = exp12 - exp26
        df["Signal_Line"] = df["MACD"].ewm(span=9, adjust=False).mean()
    
    # Generate signals
    df["Signal"] = np.where(df["MACD"] > df["Signal_Line"], 1, -1)
    
    # Position management
    df["Position"] = df["Signal"].diff()
    df.loc[df["Position"] == 0, "Position"] = np.nan
    df["Position"].fillna(method='ffill', inplace=True)
    df["Position"].fillna(0, inplace=True)
    
    # Calculate returns with slippage and commission
    df["Strategy_Return"] = df["close"].pct_change() * df["Position"].shift(1)
    trade_indices = df["Position"] != df["Position"].shift(1)
    df.loc[trade_indices, "Strategy_Return"] -= slippage
    df.loc[trade_indices, "Strategy_Return"] -= commission / initial_capital
    
    # Cumulative performance
    df["Cumulative_Return"] = (1 + df["Strategy_Return"]).cumprod()
    df["Portfolio_Value"] = initial_capital * df["Cumulative_Return"]
    
    # Calculate metrics
    total_return = df["Cumulative_Return"].iloc[-1] - 1
    max_drawdown = (df["Cumulative_Return"] / df["Cumulative_Return"].cummax() - 1).min()
    
    return df, total_return, max_drawdown

def backtest_stochastic(df, stoch_low=20, stoch_high=80, initial_capital=10000, slippage=0.001, commission=0):
    df = df.copy()
    # Calculate Stochastic if not already present
    if "%K" not in df.columns or "%D" not in df.columns:
        low_min = df["low"].rolling(window=14).min()
        high_max = df["high"].rolling(window=14).max()
        df["%K"] = 100 * ((df["close"] - low_min) / (high_max - low_min + 0.0001))
        df["%D"] = df["%K"].rolling(window=3).mean()
    
    # Generate signals
    df["Signal"] = 0
    df.loc[(df["%K"] < stoch_low) & (df["%D"] < stoch_low), "Signal"] = 1  # Buy signal (oversold)
    df.loc[(df["%K"] > stoch_high) & (df["%D"] > stoch_high), "Signal"] = -1  # Sell signal (overbought)
    
    # Position management
    df["Position"] = df["Signal"].diff()
    df.loc[df["Position"] == 0, "Position"] = np.nan
    df["Position"].fillna(method='ffill', inplace=True)
    df["Position"].fillna(0, inplace=True)
    
    # Calculate returns with slippage and commission
    df["Strategy_Return"] = df["close"].pct_change() * df["Position"].shift(1)
    trade_indices = df["Position"] != df["Position"].shift(1)
    df.loc[trade_indices, "Strategy_Return"] -= slippage
    df.loc[trade_indices, "Strategy_Return"] -= commission / initial_capital
    
    # Cumulative performance
    df["Cumulative_Return"] = (1 + df["Strategy_Return"]).cumprod()
    df["Portfolio_Value"] = initial_capital * df["Cumulative_Return"]
    
    # Calculate metrics
    total_return = df["Cumulative_Return"].iloc[-1] - 1
    max_drawdown = (df["Cumulative_Return"] / df["Cumulative_Return"].cummax() - 1).min()
    
    return df, total_return, max_drawdown

# ---------------- MONTE CARLO SIMULATION ----------------
def monte_carlo_simulation(df, initial_investment, num_simulations, num_sim_days, symbol):
    try:
        # Calculate daily returns
        if 'close' not in df.columns:
            st.error("No price data available for simulation")
            return None
            
        daily_returns = df['close'].pct_change().dropna()
        
        if daily_returns.empty:
            st.error("Not enough data to calculate returns")
            return None
            
        # Calculate drift and volatility
        u = daily_returns.mean()
        var = daily_returns.var()
        drift = u - (0.5 * var)
        stdev = daily_returns.std()
        
        # Generate simulations
        simulations = np.zeros((num_sim_days, num_simulations))
        simulations[0] = initial_investment
        
        for day in range(1, num_sim_days):
            shock = drift + stdev * norm.ppf(np.random.rand(num_simulations))
            simulations[day] = simulations[day-1] * (1 + shock)
        
        return simulations
    except Exception as e:
        st.error(f"Error in Monte Carlo simulation: {str(e)}")
        return None

# ---------------- MAIN LOGIC ----------------
if not st.session_state["authenticated"]:
    if st.session_state["mode"] == "login":
        login()
        if st.button("Create Account"):
            st.session_state["mode"] = "signup"
            st.rerun()
    else:
        signup()
        if st.button("Back to Login"):
            st.session_state["mode"] = "login"
            st.rerun()
    st.stop()

# ---------------- MAIN APP AFTER LOGIN ----------------
st.sidebar.success(f"👤 Logged in as: {st.session_state['username']}")
if st.sidebar.button("Logout"):
    st.session_state["authenticated"] = False
    st.session_state["username"] = ""
    st.session_state["mode"] = "login"
    st.session_state.real_time_active = False
    st.rerun()

# ----------------- TAB-BASED UI -----------------
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "📊 Market Analysis", 
    "🤖 Trading Strategies", 
    "📈 Portfolio Management", 
    "⚡ Real-Time & Crypto",
    "🔐 Account"
])

# ----------------- TAB 1: MARKET ANALYSIS -----------------
with tab1:
    st.title("📊 Market Analysis")
    
    with st.expander("⚙️ Select Asset & Parameters", expanded=True):
        col1, col2 = st.columns(2)
        with col1:
            # Asset type selection
            asset_type = st.radio("Asset Type", ["Stock", "Cryptocurrency"], index=0)
            is_crypto = asset_type == "Cryptocurrency"
            
            # Symbol input with crypto examples
            if is_crypto:
                symbol = st.text_input("Crypto Symbol (e.g. BTCUSDT)", value="BTCUSDT", key="symbol_tab1").upper()
            else:
                symbol = st.text_input("Stock Symbol (e.g. AAPL)", value="AAPL", key="symbol_tab1").upper()
                
            interval = st.selectbox("Interval", options=["1min", "5min", "15min", "30min", "1h", "1day"], index=5, key="interval_tab1")
            days = st.slider("Days of Data", min_value=1, max_value=365*2, value=90, key="days_tab1")
            
        with col2:
            sma_short = st.number_input("SMA Short Window", min_value=2, max_value=50, value=10, key="sma_short_tab1")
            sma_long = st.number_input("SMA Long Window", min_value=10, max_value=200, value=50, key="sma_long_tab1")
            rsi_period = st.number_input("RSI Period", min_value=2, max_value=50, value=14, key="rsi_period_tab1")
            bb_window = st.number_input("Bollinger Band Window", min_value=10, max_value=100, value=20, key="bb_window_tab1")
            bb_std = st.slider("BB Std Dev", min_value=1.0, max_value=3.0, value=2.0, step=0.1, key="bb_std_tab1")

    run_analysis = st.button("▶️ Run Market Analysis", key="run_analysis_tab1")

    if run_analysis:
        with st.spinner("Fetching data and running analysis..."):
            df = get_asset_data(symbol, interval, days, is_crypto)
            if df is not None and not df.empty:
                st.session_state.df = add_technical_indicators(df, sma_short, sma_long, rsi_period, bb_window, bb_std)
                df = st.session_state.df
                log_event(st.session_state["username"], "market_analysis", {"symbol": symbol})
                
                st.subheader(f"Technical Analysis for {symbol}")
                
                # Technical indicators dashboard
                ta_tabs = st.tabs(["Overview", "RSI", "Bollinger Bands", "MACD", "Stochastic"])
                
                with ta_tabs[0]:  # Overview
                    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, 
                                       vertical_spacing=0.03,
                                       row_heights=[0.6, 0.2, 0.2])
                    
                    # Price and SMAs
                    fig.add_trace(go.Scatter(x=df.index, y=df["close"], 
                                            mode='lines', name='Close'), row=1, col=1)
                    fig.add_trace(go.Scatter(x=df.index, y=df["SMA_Short"], 
                                            mode='lines', name=f'SMA {sma_short}'), row=1, col=1)
                    fig.add_trace(go.Scatter(x=df.index, y=df["SMA_Long"], 
                                            mode='lines', name=f'SMA {sma_long}'), row=1, col=1)
                    
                    # RSI
                    fig.add_trace(go.Scatter(x=df.index, y=df["RSI"], 
                                            mode='lines', name='RSI'), row=2, col=1)
                    fig.add_hline(y=30, line_dash="dash", row=2, col=1, line_color="green")
                    fig.add_hline(y=70, line_dash="dash", row=2, col=1, line_color="red")
                    
                    # Volume
                    if "volume" in df:
                        fig.add_trace(go.Bar(x=df.index, y=df["volume"], 
                                             name='Volume'), row=3, col=1)
                    else:
                        fig.add_trace(go.Scatter(x=df.index, y=[0]*len(df), 
                                                name='Volume (missing)'), row=3, col=1)
                    
                    fig.update_layout(height=700, title_text="Technical Overview")
                    st.plotly_chart(fig, use_container_width=True)
                
                with ta_tabs[1]:  # RSI
                    fig = go.Figure()
                    fig.add_trace(go.Scatter(x=df.index, y=df["RSI"], 
                                         mode='lines', name='RSI'))
                    fig.add_hline(y=30, line_dash="dash", line_color="green")
                    fig.add_hline(y=70, line_dash="dash", line_color="red")
                    fig.update_layout(title="RSI Indicator")
                    st.plotly_chart(fig, use_container_width=True)
                    
                    # RSI distribution
                    st.subheader("RSI Distribution")
                    fig = go.Figure(data=[go.Histogram(x=df["RSI"], nbinsx=50)])
                    fig.update_layout(title="RSI Value Distribution")
                    st.plotly_chart(fig, use_container_width=True)
                
                with ta_tabs[2]:  # Bollinger Bands
                    fig = go.Figure()
                    
                    # Price and Bollinger Bands
                    fig.add_trace(go.Scatter(x=df.index, y=df["close"], 
                                            mode='lines', name='Close', line=dict(color='royalblue')))
                    fig.add_trace(go.Scatter(x=df.index, y=df["BB_Upper"], 
                                            mode='lines', name='Upper Band', line=dict(color='crimson', dash='dash')))
                    fig.add_trace(go.Scatter(x=df.index, y=df["BB_Mid"], 
                                            mode='lines', name='Middle Band', line=dict(color='gray', dash='dot')))
                    fig.add_trace(go.Scatter(x=df.index, y=df["BB_Lower"], 
                                            mode='lines', name='Lower Band', line=dict(color='green', dash='dash')))
                    
                    # Fill between Bollinger Bands
                    fig.add_trace(go.Scatter(x=df.index.tolist() + df.index[::-1].tolist(),
                    y=df["BB_Upper"].tolist() + df["BB_Lower"][::-1].tolist(),
                    fill='toself',
                    fillcolor='rgba(200, 200, 200, 0.2)',
                    line=dict(color='rgba(255,255,255,0)'),
                    name='Bollinger Band Range'))
                    fig.update_layout(title=f"Bollinger Bands ({bb_window} periods, {bb_std}σ)",
                                    xaxis_title="Date",
                                    yaxis_title="Price")
                    st.plotly_chart(fig, use_container_width=True)
                    
                    # Bandwidth indicator
                    st.subheader("Bollinger Bandwidth")
                    bandwidth = (df["BB_Upper"] - df["BB_Lower"]) / df["BB_Mid"]
                    fig = go.Figure(go.Scatter(x=df.index, y=bandwidth, mode='lines'))
                    fig.update_layout(title="Bollinger Bandwidth Over Time")
                    st.plotly_chart(fig, use_container_width=True)
                
                with ta_tabs[3]:  # MACD
                    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, 
                                      vertical_spacing=0.1,
                                      row_heights=[0.7, 0.3])
                    
                    # Price
                    fig.add_trace(go.Scatter(x=df.index, y=df["close"], 
                                          mode='lines', name='Close'), row=1, col=1)
                    
                    # MACD
                    fig.add_trace(go.Scatter(x=df.index, y=df["MACD"], 
                                          mode='lines', name='MACD', line=dict(color='blue')), row=2, col=1)
                    fig.add_trace(go.Scatter(x=df.index, y=df["Signal_Line"], 
                                          mode='lines', name='Signal', line=dict(color='orange')), row=2, col=1)
                    
                    # Histogram
                    colors = ['green' if val >= 0 else 'red' for val in df["MACD_Hist"]]
                    fig.add_trace(go.Bar(x=df.index, y=df["MACD_Hist"], 
                                       name='Histogram', marker_color=colors), row=2, col=1)
                    
                    # Zero line
                    fig.add_hline(y=0, line_dash="dash", row=2, col=1, line_color="gray")
                    
                    fig.update_layout(height=600, title_text="MACD Indicator")
                    st.plotly_chart(fig, use_container_width=True)
                    
                    # MACD crossover signals
                    st.subheader("MACD Crossovers")
                    df["MACD_Signal"] = np.where(df["MACD"] > df["Signal_Line"], 1, -1)
                    df["MACD_Crossover"] = df["MACD_Signal"].diff()
                    crossovers = df[df["MACD_Crossover"] != 0]
                    
                    if not crossovers.empty:
                        fig = go.Figure()
                        fig.add_trace(go.Scatter(x=df.index, y=df["close"], 
                                              mode='lines', name='Close'))
                        
                        # Add buy signals (MACD crosses above signal)
                        buy_signals = crossovers[crossovers["MACD_Crossover"] > 0]
                        fig.add_trace(go.Scatter(x=buy_signals.index, y=buy_signals["close"],
                                              mode='markers', name='Buy Signal',
                                              marker=dict(color='green', size=10)))
                        
                        # Add sell signals (MACD crosses below signal)
                        sell_signals = crossovers[crossovers["MACD_Crossover"] < 0]
                        fig.add_trace(go.Scatter(x=sell_signals.index, y=sell_signals["close"],
                                              mode='markers', name='Sell Signal',
                                              marker=dict(color='red', size=10)))
                        
                        fig.update_layout(title="MACD Crossover Signals")
                        st.plotly_chart(fig, use_container_width=True)
                    else:
                        st.info("No MACD crossovers detected in this period")
                
                with ta_tabs[4]:  # Stochastic
                    fig = go.Figure()
                    
                    # %K and %D lines
                    fig.add_trace(go.Scatter(x=df.index, y=df["%K"], 
                                          mode='lines', name='%K (Fast)', line=dict(color='blue')))
                    fig.add_trace(go.Scatter(x=df.index, y=df["%D"], 
                                          mode='lines', name='%D (Slow)', line=dict(color='orange')))
                    
                    # Overbought/oversold lines
                    fig.add_hline(y=80, line_dash="dash", line_color="red")
                    fig.add_hline(y=20, line_dash="dash", line_color="green")
                    
                    fig.update_layout(title="Stochastic Oscillator",
                                    xaxis_title="Date",
                                    yaxis_title="Value")
                    st.plotly_chart(fig, use_container_width=True)
                    
                    # Stochastic signals
                    st.subheader("Stochastic Signals")
                    df["Stoch_Signal"] = 0
                    df.loc[(df["%K"] < 20) & (df["%D"] < 20), "Stoch_Signal"] = 1  # Oversold
                    df.loc[(df["%K"] > 80) & (df["%D"] > 80), "Stoch_Signal"] = -1  # Overbought
                    
                    signals = df[df["Stoch_Signal"] != 0]
                    
                    if not signals.empty:
                        fig = go.Figure()
                        fig.add_trace(go.Scatter(x=df.index, y=df["close"], 
                                              mode='lines', name='Close'))
                        
                        # Add buy signals (oversold)
                        buy_signals = signals[signals["Stoch_Signal"] > 0]
                        fig.add_trace(go.Scatter(x=buy_signals.index, y=buy_signals["close"],
                                              mode='markers', name='Oversold Buy',
                                              marker=dict(color='green', size=10, symbol='triangle-up')))
                        
                        # Add sell signals (overbought)
                        sell_signals = signals[signals["Stoch_Signal"] < 0]
                        fig.add_trace(go.Scatter(x=sell_signals.index, y=sell_signals["close"],
                                              mode='markers', name='Overbought Sell',
                                              marker=dict(color='red', size=10, symbol='triangle-down')))
                        
                        fig.update_layout(title="Stochastic Trading Signals")
                        st.plotly_chart(fig, use_container_width=True)
                    else:
                        st.info("No strong stochastic signals detected in this period")
            else:
                st.error("Failed to fetch asset data. Please try a different symbol or interval.")

# ----------------- TAB 2: TRADING STRATEGIES -----------------
with tab2:
    st.title("🤖 Trading Strategies")
    
    # Create a safe reference to the dataframe
    df = None
    if 'df' in st.session_state and st.session_state.df is not None:
        df = st.session_state.df.copy()
    
    # If we don't have valid data, show warning and stop
    if df is None or df.empty:
        st.warning("Please run market analysis in the Market Analysis tab first")
        st.stop()
    
    col1, col2 = st.columns(2)
    with col1:
        strategy_type = st.selectbox("Select Strategy", [
            "SMA Crossover", 
            "RSI Mean Reversion",
            "Bollinger Bands Reversion",
            "MACD Crossover",
            "Stochastic Oscillator"
        ], key="strategy_type_tab2")
        
    with col2:
        initial_capital = st.number_input("Initial Capital ($)", 
                                        min_value=1000, max_value=1_000_000, 
                                        value=10000, step=1000, key="initial_capital_tab2")
        slippage = st.slider("Slippage (%)", min_value=0.0, max_value=1.0, value=0.1, step=0.01, key="slippage_tab2")/100
        commission = st.slider("Commission per Trade ($)", min_value=0.0, max_value=10.0, value=0.0, step=0.5, key="commission_tab2")
    
    # Strategy-specific parameters
    if strategy_type == "SMA Crossover":
        col1, col2 = st.columns(2)
        with col1:
            sma_short = st.number_input("SMA Short Window", min_value=2, max_value=50, value=10, key="sma_short_tab2")
        with col2:
            sma_long = st.number_input("SMA Long Window", min_value=10, max_value=200, value=50, key="sma_long_tab2")
        
    elif strategy_type == "RSI Mean Reversion":
        col1, col2 = st.columns(2)
        with col1:
            rsi_low = st.slider("RSI Buy Threshold", 0, 50, 30, key="rsi_low_tab2")
        with col2:
            rsi_high = st.slider("RSI Sell Threshold", 50, 100, 70, key="rsi_high_tab2")
        rsi_period = st.number_input("RSI Period", min_value=2, max_value=50, value=14, key="rsi_period_tab2")
        
    elif strategy_type == "Bollinger Bands Reversion":
        col1, col2 = st.columns(2)
        with col1:
            bb_window = st.number_input("BB Window", min_value=10, max_value=100, value=20, key="bb_window_tab2")
        with col2:
            bb_std = st.slider("BB Standard Deviations", 1.0, 3.0, 2.0, step=0.1, key="bb_std_tab2")
            
    elif strategy_type == "MACD Crossover":
        st.info("MACD parameters are fixed at 12, 26, 9")
    
    elif strategy_type == "Stochastic Oscillator":
        col1, col2 = st.columns(2)
        with col1:
            stoch_low = st.slider("Stochastic Buy Threshold", 0, 30, 20, key="stoch_low_tab2")
        with col2:
            stoch_high = st.slider("Stochastic Sell Threshold", 70, 100, 80, key="stoch_high_tab2")
    
    if st.button("Backtest Strategy", key="backtest_button_tab2"):
        with st.spinner("Running backtest..."):
            try:
                # Run appropriate backtest based on strategy
                if strategy_type == "SMA Crossover":
                    result, total_return, max_drawdown = backtest_sma(df, sma_short, sma_long, initial_capital, slippage, commission)
                
                elif strategy_type == "RSI Mean Reversion":
                    result, total_return, max_drawdown = backtest_rsi(df, rsi_low, rsi_high, initial_capital, slippage, commission)
                
                elif strategy_type == "Bollinger Bands Reversion":
                    result, total_return, max_drawdown = backtest_bollinger_bands(df, bb_window, bb_std, initial_capital, slippage, commission)
                
                elif strategy_type == "MACD Crossover":
                    result, total_return, max_drawdown = backtest_macd(df, initial_capital, slippage, commission)
                
                elif strategy_type == "Stochastic Oscillator":
                    result, total_return, max_drawdown = backtest_stochastic(df, stoch_low, stoch_high, initial_capital, slippage, commission)
                
                st.session_state.backtest_result = result
                log_event(st.session_state["username"], "strategy_backtest", {"strategy": strategy_type})
                
                # Display results
                st.subheader("Backtest Results")
                col1, col2, col3 = st.columns(3)
                col1.metric("Total Return", f"{total_return*100:.2f}%")
                col2.metric("Max Drawdown", f"{max_drawdown*100:.2f}%")
                col3.metric("Final Value", f"${result['Portfolio_Value'].iloc[-1]:,.2f}")
                
                # Portfolio value chart
                fig = go.Figure()
                fig.add_trace(go.Scatter(x=result.index, y=result["Portfolio_Value"], 
                                        mode='lines', name='Strategy'))
                fig.add_trace(go.Scatter(x=df.index, y=initial_capital * (1 + df["close"].pct_change().fillna(0).cumsum()), 
                                        mode='lines', name='Buy & Hold'))
                fig.update_layout(title="Portfolio Value Over Time")
                st.plotly_chart(fig, use_container_width=True)
                
                # Trade log
                if "Position" in result:
                    trades = result[result["Position"] != result["Position"].shift(1)]
                    if not trades.empty:
                        st.subheader("Trade History")
                        trades_display = trades[["close", "Position"]].copy()
                        trades_display = trades_display.rename(columns={"close": "Price"})
                        trades_display["Action"] = trades_display["Position"].map({1: "BUY", -1: "SELL"})
                        trades_display["Trade Value"] = trades_display["Price"] * (initial_capital / trades_display["Price"].iloc[0])
                        st.dataframe(trades_display[["Price", "Action", "Trade Value"]])
                    else:
                        st.info("No trades executed during this period")
                
                # Strategy-specific visualizations
                st.subheader("Strategy Visualization")
                
                if strategy_type == "SMA Crossover":
                    fig = go.Figure()
                    fig.add_trace(go.Scatter(x=result.index, y=result["close"], name='Price'))
                    fig.add_trace(go.Scatter(x=result.index, y=result["SMA_Short"], name=f'SMA {sma_short}'))
                    fig.add_trace(go.Scatter(x=result.index, y=result["SMA_Long"], name=f'SMA {sma_long}'))
                    
                    # Add buy/sell signals
                    buy_signals = result[result["Position"] > 0]
                    sell_signals = result[result["Position"] < 0]
                    if not buy_signals.empty:
                        fig.add_trace(go.Scatter(x=buy_signals.index, y=buy_signals["close"],
                                                mode='markers', name='Buy', marker=dict(color='green', size=10)))
                    if not sell_signals.empty:
                        fig.add_trace(go.Scatter(x=sell_signals.index, y=sell_signals["close"],
                                                mode='markers', name='Sell', marker=dict(color='red', size=10)))
                    fig.update_layout(title="SMA Crossover Strategy Signals")
                    st.plotly_chart(fig, use_container_width=True)
                
                elif strategy_type == "Bollinger Bands Reversion":
                    fig = go.Figure()
                    fig.add_trace(go.Scatter(x=result.index, y=result["close"], name='Price'))
                    fig.add_trace(go.Scatter(x=result.index, y=result["BB_Upper"], name='Upper Band', line=dict(dash='dash')))
                    fig.add_trace(go.Scatter(x=result.index, y=result["BB_Lower"], name='Lower Band', line=dict(dash='dash')))
                    
                    # Add buy/sell signals
                    buy_signals = result[result["Position"] > 0]
                    sell_signals = result[result["Position"] < 0]
                    if not buy_signals.empty:
                        fig.add_trace(go.Scatter(x=buy_signals.index, y=buy_signals["close"],
                                                mode='markers', name='Buy', marker=dict(color='green', size=10)))
                    if not sell_signals.empty:
                        fig.add_trace(go.Scatter(x=sell_signals.index, y=sell_signals["close"],
                                                mode='markers', name='Sell', marker=dict(color='red', size=10)))
                    fig.update_layout(title="Bollinger Bands Strategy Signals")
                    st.plotly_chart(fig, use_container_width=True)
                
                elif strategy_type == "MACD Crossover":
                    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.7, 0.3])
                    fig.add_trace(go.Scatter(x=result.index, y=result["close"], name='Price'), row=1, col=1)
                    fig.add_trace(go.Scatter(x=result.index, y=result["MACD"], name='MACD'), row=2, col=1)
                    fig.add_trace(go.Scatter(x=result.index, y=result["Signal_Line"], name='Signal Line'), row=2, col=1)
                    
                    # Add signals
                    buy_signals = result[result["Position"] > 0]
                    sell_signals = result[result["Position"] < 0]
                    if not buy_signals.empty:
                        fig.add_trace(go.Scatter(x=buy_signals.index, y=buy_signals["close"],
                                                mode='markers', name='Buy', marker=dict(color='green', size=10)), row=1, col=1)
                    if not sell_signals.empty:
                        fig.add_trace(go.Scatter(x=sell_signals.index, y=sell_signals["close"],
                                                mode='markers', name='Sell', marker=dict(color='red', size=10)), row=1, col=1)
                    fig.update_layout(height=600, title_text="MACD Crossover Strategy")
                    st.plotly_chart(fig, use_container_width=True)
                
                elif strategy_type == "Stochastic Oscillator":
                    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.7, 0.3])
                    fig.add_trace(go.Scatter(x=result.index, y=result["close"], name='Price'), row=1, col=1)
                    fig.add_trace(go.Scatter(x=result.index, y=result["%K"], name='%K'), row=2, col=1)
                    fig.add_trace(go.Scatter(x=result.index, y=result["%D"], name='%D'), row=2, col=1)
                    fig.add_hline(y=stoch_high, line_dash="dash", row=2, col=1, line_color="red")
                    fig.add_hline(y=stoch_low, line_dash="dash", row=2, col=1, line_color="green")
                    
                    # Add signals
                    buy_signals = result[result["Position"] > 0]
                    sell_signals = result[result["Position"] < 0]
                    if not buy_signals.empty:
                        fig.add_trace(go.Scatter(x=buy_signals.index, y=buy_signals["close"],
                                                mode='markers', name='Buy', marker=dict(color='green', size=10)), row=1, col=1)
                    if not sell_signals.empty:
                        fig.add_trace(go.Scatter(x=sell_signals.index, y=sell_signals["close"],
                                                mode='markers', name='Sell', marker=dict(color='red', size=10)), row=1, col=1)
                    fig.update_layout(height=600, title_text="Stochastic Strategy")
                    st.plotly_chart(fig, use_container_width=True)
                
                # Performance metrics table
                st.subheader("Performance Metrics")
                trades = result[result["Position"] != result["Position"].shift(1)] if "Position" in result else []
                num_trades = len(trades) if not trades.empty else 0
                
                metrics = {
                    "Total Return": f"{total_return*100:.2f}%",
                    "Max Drawdown": f"{max_drawdown*100:.2f}%",
                    "Final Portfolio Value": f"${result['Portfolio_Value'].iloc[-1]:,.2f}",
                    "Number of Trades": num_trades,
                    "Average Trade Return": f"{total_return/(num_trades if num_trades > 0 else 1)*100:.2f}%" if num_trades > 0 else "N/A",
                    "Win Rate": "N/A"  # Could be implemented with more detailed trade analysis
                }
                st.table(pd.DataFrame(list(metrics.items()), columns=["Metric", "Value"]))
                
            except Exception as e:
                st.error(f"Error during backtesting: {str(e)}")
                st.stop()

# ----------------- TAB 3: PORTFOLIO MANAGEMENT -----------------
with tab3:
    st.title("📈 Portfolio Management")
    
    # Get current symbol from session state
    symbol = st.session_state.get("symbol", "AAPL")
    is_crypto = st.session_state.get("is_crypto", False)
    
    initial_investment = st.number_input("Initial Investment ($)", 
                                       min_value=1000, max_value=1_000_000, 
                                       value=10000, step=1000, key="initial_investment_tab3")
    num_simulations = st.number_input("Number of Monte Carlo Simulations", 
                                    min_value=10, max_value=1000, value=100, step=10, key="num_simulations_tab3")
    num_sim_days = st.number_input("Days to Simulate", min_value=10, max_value=365, value=60, key="num_sim_days_tab3")
    
    run_sim = st.button("▶️ Run Portfolio Simulation", key="run_sim_tab3")
    
    if run_sim and st.session_state.df is not None:
        with st.spinner("Running Monte Carlo simulation..."):
            simulations = monte_carlo_simulation(
                st.session_state.df, 
                initial_investment, 
                num_simulations, 
                num_sim_days,
                symbol  # Pass the symbol parameter
            )
            
            if simulations is not None:
                st.session_state.simulations = simulations
                log_event(st.session_state["username"], "portfolio_simulation", {})
                
                st.subheader("Monte Carlo Simulation of Portfolio Value")
                fig3 = go.Figure()
                for i in range(min(50, num_simulations)):
                    fig3.add_trace(go.Scatter(y=simulations[:, i], 
                                             mode="lines", 
                                             line=dict(width=1), 
                                             opacity=0.3, 
                                             showlegend=False))
                fig3.update_layout(title="Monte Carlo Price Paths",
                                  xaxis_title="Days",
                                  yaxis_title="Portfolio Value ($)")
                st.plotly_chart(fig3, use_container_width=True)
                
                # Calculate statistics
                final_values = simulations[-1, :]
                avg_final = np.mean(final_values)
                pct_90 = np.percentile(final_values, 90)
                pct_10 = np.percentile(final_values, 10)
                
                st.subheader("📊 Simulation Results")
                col1, col2, col3 = st.columns(3)
                col1.metric("Average Final Value", f"${avg_final:,.2f}")
                col2.metric("90th Percentile", f"${pct_90:,.2f}")
                col3.metric("10th Percentile", f"${pct_10:,.2f}")
    
    # Portfolio saving
    st.subheader("💾 Save Portfolio")
    portfolio_name = st.text_input("Portfolio Name", key="portfolio_name_tab3")
    if st.button("Save Current Portfolio", key="save_portfolio_tab3"):
        if portfolio_name and st.session_state.df is not None:
            portfolio_data = {
                "symbol": symbol,
                "is_crypto": is_crypto,
                "interval": interval,
                "days": days,
                "sma_short": sma_short,
                "sma_long": sma_long,
                "rsi_period": rsi_period,
                "bb_window": bb_window,
                "bb_std": bb_std,
                "initial_investment": initial_investment,
                "num_simulations": num_simulations,
                "num_sim_days": num_sim_days,
                "data": st.session_state.df.tail(100).to_dict(),
            }
            save_portfolio(st.session_state["username"], portfolio_name, portfolio_data)
            st.success(f"Portfolio '{portfolio_name}' saved!")
    
    st.subheader("💾 Save Current Simulation")
    sim_name = st.text_input("Simulation Name", key="sim_name_tab3")
    if st.button("Save Simulation", key="save_simulation_tab3") and st.session_state.simulations is not None:
        save_simulation(st.session_state["username"], portfolio_name or "Unnamed Portfolio", st.session_state.simulations)
        st.success(f"Simulation saved under portfolio '{portfolio_name or 'Unnamed Portfolio'}.")

# ----------------- TAB 4: REAL-TIME & CRYPTO ANALYSIS -----------------
with tab4:
    st.title("⚡ Real-Time & Crypto Analysis")
    
    col1, col2 = st.columns(2)
    with col1:
        # Asset type selection
        asset_type = st.radio("Asset Type", ["Stock", "Cryptocurrency"], index=0, key="rt_asset_type")
        is_crypto = asset_type == "Cryptocurrency"
        
        # Symbol input
        if is_crypto:
            symbol = st.selectbox("Cryptocurrency Pair", 
                               ["BTC/USD", "ETH/USD", "BNB/USD", "XRP/USD", "SOL/USD"],
                               index=0, key="rt_crypto_symbol")
        else:
            symbol = st.text_input("Stock Symbol (e.g. AAPL)", value="AAPL", key="rt_stock_symbol").upper()
        
        # Interval selection
        rt_interval = st.selectbox("Interval", 
                                  ["1min", "5min", "15min", "30min", "1h"], 
                                  index=0,
                                  key="rt_interval")
        
        # Real-time controls
        if st.button("▶️ Start Real-Time Stream") and not st.session_state.real_time_active:
            # For crypto, we'll use simulated real-time since Twelve Data doesn't offer WebSockets
            if is_crypto:
                st.session_state.real_time_active = True
                st.session_state.real_time_data = get_asset_data(symbol.split("/")[0] + "USD", "1min", 1, True).tail(30)
                st.info("Using simulated real-time for crypto (refresh every 10s)")
            else:
                # For stocks, use the existing method
                start_real_time(symbol, rt_interval, is_crypto)
            log_event(st.session_state["username"], "realtime_start", {"symbol": symbol})
            
        if st.button("⏹️ Stop Real-Time Stream") and st.session_state.real_time_active:
            st.session_state.real_time_active = False
            
    with col2:
        st.subheader("Real-Time Data")
        if not st.session_state.real_time_data.empty:
            latest_data = st.session_state.real_time_data.iloc[-1]
            st.metric("Latest Price", f"${latest_data['close']:,.2f}")
            st.metric("Volume", f"{latest_data['volume']:,.2f}")
            
            # Calculate and display price change
            if len(st.session_state.real_time_data) > 1:
                prev_close = st.session_state.real_time_data.iloc[-2]['close']
                price_change = latest_data['close'] - prev_close
                pct_change = (price_change / prev_close) * 100
                st.metric("Price Change", f"${price_change:,.2f}", f"{pct_change:.2f}%")
        else:
            st.info("No real-time data yet. Start the stream to begin receiving data.")
    
    # Display real-time chart if data exists
    if not st.session_state.real_time_data.empty:
        st.subheader("Real-Time Price Chart")
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=st.session_state.real_time_data.index, 
                                 y=st.session_state.real_time_data['close'],
                                 mode='lines+markers',
                                 name='Price'))
        fig.update_layout(title=f"Real-Time {symbol} Price",
                         xaxis_title="Time",
                         yaxis_title="Price (USD)")
        st.plotly_chart(fig, use_container_width=True)
        
        # Simulate real-time updates for crypto
        if is_crypto and st.session_state.real_time_active:
            time.sleep(10)
            new_data = get_asset_data(symbol.split("/")[0] + "USD", "1min", 1, True).tail(1)
            if not new_data.empty:
                st.session_state.real_time_data = pd.concat([
                    st.session_state.real_time_data, 
                    new_data
                ]).tail(30)  # Keep only last 30 data points
                st.rerun()
    
    # Historical crypto analysis
    st.subheader("Cryptocurrency Historical Analysis")
    crypto_symbol_hist = st.selectbox("Select Cryptocurrency", 
                                    ["BTC/USD", "ETH/USD", "BNB/USD", "XRP/USD", "SOL/USD"],
                                    index=0,
                                    key="crypto_hist")
    crypto_days = st.slider("Days of Data", min_value=1, max_value=365, value=30, key="crypto_days")
    crypto_interval = st.selectbox("Data Interval", ["1day", "1h", "30min"], index=0, key="crypto_interval")
    
    if st.button("Analyze Cryptocurrency", key="analyze_crypto"):
        with st.spinner("Fetching cryptocurrency data..."):
            # Use our new get_asset_data function with Twelve Data
            df_crypto = get_asset_data(crypto_symbol_hist, crypto_interval, crypto_days, True)
            if df_crypto is not None and not df_crypto.empty:
                # Add technical indicators
                df_crypto = add_technical_indicators(df_crypto, 10, 50, 14, 20, 2)
                log_event(st.session_state["username"], "crypto_analysis", {"symbol": crypto_symbol_hist})
                
                st.subheader(f"Technical Analysis for {crypto_symbol_hist}")
                
                # Price and indicators
                fig = make_subplots(rows=2, cols=1, shared_xaxes=True, 
                                   vertical_spacing=0.05,
                                   row_heights=[0.7, 0.3])
                
                # Price
                fig.add_trace(go.Scatter(x=df_crypto.index, y=df_crypto['close'], 
                                        name='Price', line=dict(color='blue')), row=1, col=1)
                
                # Volume
                fig.add_trace(go.Bar(x=df_crypto.index, y=df_crypto['volume'], 
                                     name='Volume', marker=dict(color='green')), row=2, col=1)
                
                fig.update_layout(height=600, title_text="Price and Volume")
                st.plotly_chart(fig, use_container_width=True)
                
                # Volatility analysis
                st.subheader("Volatility Analysis")
                df_crypto['daily_returns'] = df_crypto['close'].pct_change()
                fig = go.Figure()
                fig.add_trace(go.Histogram(x=df_crypto['daily_returns'].dropna(), nbinsx=50))
                fig.update_layout(title="Daily Returns Distribution",
                                 xaxis_title="Daily Return",
                                 yaxis_title="Frequency")
                st.plotly_chart(fig, use_container_width=True)
                
                # Calculate volatility metrics
                daily_vol = df_crypto['daily_returns'].std()
                annual_vol = daily_vol * np.sqrt(365)
                st.metric("Daily Volatility", f"{daily_vol*100:.2f}%")
                st.metric("Annualized Volatility", f"{annual_vol*100:.2f}%")
            else:
                st.error("Failed to fetch cryptocurrency data. Please try again later.")

# ----------------- TAB 5: ACCOUNT MANAGEMENT -----------------
with tab5:
    st.title("🔐 Account Management")
    
    # Subscription status
    tier, remaining = check_usage_limit(st.session_state["username"])
    st.subheader(f"Subscription: {tier.capitalize()} Tier")
    st.write(f"GPU Hours Remaining: **{remaining}**")
    
    # User profile
    st.subheader("👤 Your Profile")
    user_info = get_user(st.session_state["username"])
    if user_info:
        st.write(f"**Username:** {user_info[0]}")
        st.write(f"**Email:** {user_info[2]}")
        st.write(f"**Joined:** {user_info[3]}")
    
    # Feedback system
    st.subheader("💬 Feedback & Suggestions")
    st.write("We value your input! Help us improve QuantWise by sharing your experience.")
    
    rating = st.slider("How would you rate your experience with QuantWise?", 
                     1, 5, 3, 
                     help="1 = Poor, 5 = Excellent")
    
    feedback = st.text_area("Your feedback, suggestions, or feature requests", 
                          height=150,
                          placeholder="What do you like about QuantWise? What can we improve?")
    
    contact_ok = st.checkbox("I'm open to being contacted about my feedback")
    
    if st.button("Submit Feedback"):
        if not feedback.strip():
            st.warning("Please provide some feedback before submitting")
        else:
            save_feedback(st.session_state["username"], rating, feedback)
            
            # Store contact preference
            if contact_ok:
                log_event(st.session_state["username"], "feedback_contact_ok", {})
            
            st.success("Thank you for your feedback! We'll use this to improve QuantWise.")
            
            # Show confirmation
            st.balloons()
            
    if st.session_state["username"] == "admin":
      with st.expander("Admin Dashboard", expanded=False):
        st.title("📊 Feedback Dashboard")
        
        conn = get_db_connection()
        
        # Feedback summary
        st.subheader("Feedback Summary")
        feedback_data = pd.read_sql("SELECT * FROM user_feedback", conn)
        
        if not feedback_data.empty:
            # Average rating
            avg_rating = feedback_data["rating"].mean()
            st.metric("Average Rating", f"{avg_rating:.1f}/5")
            
            # Rating distribution
            st.subheader("Rating Distribution")
            rating_counts = feedback_data["rating"].value_counts().sort_index()
            fig = px.bar(rating_counts, 
                        x=rating_counts.index, 
                        y=rating_counts.values,
                        labels={'x': 'Rating', 'y': 'Count'},
                        title="User Ratings")
            st.plotly_chart(fig)
            
            # Recent feedback
            st.subheader("Recent Feedback")
            st.dataframe(feedback_data[["username", "rating", "feedback", "timestamp"]])
            
            # Feedback word cloud (optional)
            try:
                
                text = " ".join(feedback_data["feedback"].dropna())
                if text:
                    wordcloud = WordCloud(width=800, height=400).generate(text)
                    st.subheader("Feedback Word Cloud")
                    st.image(wordcloud.to_array())
            except:
                pass
        else:
            st.info("No feedback submitted yet")
        
        conn.close()

st.subheader("API Status")
    
    
