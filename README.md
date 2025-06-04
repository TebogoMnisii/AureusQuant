# ⚡ QuantWise

**QuantWise** is an institutional-grade analytics and risk modeling platform designed for both retail investors and fintech builders. It integrates portfolio optimization, backtesting engines, technical indicators, and real-time crypto trading data in one cloud-native, GPU-accelerated environment.

Live App: [quantwise.streamlit.app](https://quantwise.streamlit.app)

---

## 🚀 Key Features

- 📊 **Market Analysis** with SMA, RSI, MACD, Bollinger Bands, and Stochastic Oscillator  
- 🤖 **Backtesting Framework** for SMA, RSI, MACD, Bollinger Bands, and Stochastic strategies  
- 🧠 **Real-Time Crypto Data** streaming via Binance WebSocket  
- 📈 **Monte Carlo Simulation** for future portfolio value projections  
- 🧮 **FICO Score Bucketing & Credit Scoring** with SHAP explanations  
- 🔐 **User Auth System** (SQLite + bcrypt + beta invite codes)  
- 💻 **Dashboard UI** powered by Streamlit + Plotly  
- ⚙️ **GPU support** (Numba, CUDA-ready architecture)  

---

## 📂 App Structure

- `Market Analysis` – Historical price analysis + indicators  
- `Trading Strategies` – Strategy selection + custom parameter backtesting  
- `Portfolio Simulation` – Monte Carlo simulation using historical volatility  
- `Real-Time & Crypto` – WebSocket live crypto data + visual charts  
- `Account` – Signup/Login with usage metering and GPU hour tracking  

---

## 🔐 Auth System & Subscriptions

- Beta invite code protection  
- Email + password registration  
- SQLite-powered user DB with hashed credentials  
- GPU hour limits by subscription tier (`free`, `pro`, etc.)  
- Logs usage in an audit trail table

---

## 🛠️ Stack & Dependencies

```text
Frontend:
- Streamlit
- Plotly
- WordCloud

Backend:
- Python
- SQLite
- FastAPI (for modular API build-out)
- CVXPY (optimization)
- ARCH (GARCH modeling)
- Binance API + Twelve Data API
- Numba/CUDA (for simulation acceleration)

Security & Utils:
- bcrypt
- threading / asyncio
- smtplib (email notifications)
- dotenv (env config)
