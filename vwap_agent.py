import os
import time
import logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import List, Dict, Optional

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# ------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------
API_KEY = os.getenv('APCA_API_KEY_ID')          # Alpaca Paper API Key ID
SECRET_KEY = os.getenv('APCA_API_SECRET_KEY')   # Alpaca Paper Secret Key
BASE_URL = 'https://paper-api.alpaca.markets'   # Paper trading URL

SYMBOLS = ['SPY', 'QQQ', 'IWM', 'AAPL', 'MSFT', 'NVDA', 'AMD', 'TSLA', 'META', 'AMZN']
MAX_POSITIONS = 4
MAX_TOTAL_EXPOSURE = 500.0          # $500
ENTRY_NOTIONAL = 100.0              # $100 per entry
COOLDOWN_SECONDS = 600              # 10 minutes
VWAP_ENTRY_DISTANCE = 0.005         # 0.5% below VWAP
VWAP_EXIT_DISTANCE = 0.001          # within 0.1% of VWAP
VOLUME_RATIO_THRESHOLD = 1.2
MAX_HOLD_MINUTES = 15
LOSS_EXIT_PERCENT = -0.005          # -0.5%

PAPER_TRADING = True                # Must be True for hackathon
DRY_RUN = True                      # Start with dry-run (no orders)
REPLAY_MODE = False                 # Not used in minimal version

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('agent.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('VWAPAgent')

# ------------------------------------------------------------
# ALPACA CLIENTS
# ------------------------------------------------------------
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER_TRADING)

# ------------------------------------------------------------
# 1. MARKET DATA AGENT
# ------------------------------------------------------------
class MarketDataAgent:
    """Fetches data and computes indicators."""
    def __init__(self, symbols: List[str]):
        self.symbols = symbols

    def get_bars(self, symbol: str, limit: int = 200) -> Optional[pd.DataFrame]:
        """Fetch recent completed 1-minute bars."""
        try:
            now = datetime.now()
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Minute,
                start=now - timedelta(minutes=limit),
                end=now,
                limit=limit
            )
            bars = data_client.get_stock_bars(request)
            df = bars.df.reset_index()
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            df = df.set_index('timestamp')
            return df
        except Exception as e:
            logger.error(f"Error fetching bars for {symbol}: {e}")
            return None

    def compute_indicators(self, df: pd.DataFrame, symbol: str) -> Optional[Dict]:
        """Compute session VWAP, last price, distance, volume ratio."""
        if df is None or df.empty:
            return None

        # Use only today's data for session VWAP
        today = pd.Timestamp.now().date()
        session_df = df[df.index.date == today]
        if session_df.empty:
            session_df = df

        typical_price = (session_df['high'] + session_df['low'] + session_df['close']) / 3
        cum_vol = session_df['volume'].cumsum()
        vwap_series = (typical_price * session_df['volume']).cumsum() / cum_vol
        current_vwap = vwap_series.iloc[-1]
        last_price = session_df['close'].iloc[-1]
        distance_from_vwap = (last_price - current_vwap) / current_vwap

        rolling_vol_20 = session_df['volume'].rolling(20).mean().iloc[-1]
        current_volume = session_df['volume'].iloc[-1]
        volume_ratio = current_volume / rolling_vol_20 if rolling_vol_20 else 0

        return {
            'symbol': symbol,
            'last_price': last_price,
            'vwap': current_vwap,
            'distance_from_vwap': distance_from_vwap,
            'volume_ratio': volume_ratio,
            'timestamp': session_df.index[-1]
        }

# ------------------------------------------------------------
# 2. STRATEGY AGENT
# ------------------------------------------------------------
class StrategyAgent:
    """Converts market data into BUY/SELL/HOLD proposals."""
    def __init__(self, mda: MarketDataAgent):
        self.mda = mda

    def generate_proposals(self) -> List[Dict]:
        """Evaluate all symbols and return trade proposals."""
        proposals = []
        positions = trading_client.get_all_positions()
        position_symbols = {p.symbol: p for p in positions}

        for symbol in SYMBOLS:
            bars = self.mda.get_bars(symbol)
            indicators = self.mda.compute_indicators(bars, symbol)
            if not indicators:
                continue

            # If we already hold this symbol, check exit conditions
            if symbol in position_symbols:
                pos = position_symbols[symbol]
                entry_price = float(pos.avg_entry_price)
                current_price = indicators['last_price']

                # Exit condition 1: price near VWAP (within 0.1%)
                if indicators['distance_from_vwap'] >= -VWAP_EXIT_DISTANCE:
                    proposals.append({
                        'symbol': symbol,
                        'side': 'SELL',
                        'reason': 'Price returned near VWAP',
                        'quantity': float(pos.qty)
                    })
                    continue

                # Exit condition 2: stop loss (loss > 0.5%)
                loss_pct = (current_price - entry_price) / entry_price
                if loss_pct <= LOSS_EXIT_PERCENT:
                    proposals.append({
                        'symbol': symbol,
                        'side': 'SELL',
                        'reason': f'Stop loss hit ({loss_pct:.2%})',
                        'quantity': float(pos.qty)
                    })
                    continue

                # Exit condition 3: holding time > 15 minutes (simplified: not implemented)
                # We skip time-based exit for minimal version
                continue  # HOLD

            # Entry condition for new positions
            if len(positions) >= MAX_POSITIONS:
                continue

            if (indicators['distance_from_vwap'] <= -VWAP_ENTRY_DISTANCE and
                indicators['volume_ratio'] >= VOLUME_RATIO_THRESHOLD):
                proposals.append({
                    'symbol': symbol,
                    'side': 'BUY',
                    'reason': f"Price {indicators['distance_from_vwap']:.2%} below VWAP, volume ratio {indicators['volume_ratio']:.2f}",
                    'notional': ENTRY_NOTIONAL
                })
        return proposals

# ------------------------------------------------------------
# 3. RISK OVERSIGHT AGENT
# ------------------------------------------------------------
class RiskOversightAgent:
    """Approves/rejects proposals based on risk limits."""
    def __init__(self):
        self.max_positions = MAX_POSITIONS
        self.max_exposure = MAX_TOTAL_EXPOSURE
        self.cooldown = COOLDOWN_SECONDS

    def review(self, proposals: List[Dict]) -> List[Dict]:
        """Return only approved proposals."""
        approved = []
        positions = trading_client.get_all_positions()
        current_positions = len(positions)
        total_market_value = sum(float(p.market_value or 0) for p in positions)
        last_order_times = self._get_last_order_times()

        for prop in proposals:
            symbol = prop['symbol']
            side = prop['side']

            # Check max positions (for BUY)
            if side == 'BUY' and current_positions >= self.max_positions:
                logger.warning(f"Rejected BUY {symbol}: max positions reached")
                continue

            # Check total exposure (for BUY)
            if side == 'BUY' and total_market_value + prop.get('notional', 0) > self.max_exposure:
                logger.warning(f"Rejected BUY {symbol}: exposure limit")
                continue

            # Check cooldown (for BUY)
            if side == 'BUY' and symbol in last_order_times:
                last_time = last_order_times[symbol]
                if (datetime.now() - last_time).total_seconds() < self.cooldown:
                    logger.warning(f"Rejected BUY {symbol}: cooldown active")
                    continue

            # Check duplicate open orders (simplified: skip)
            prop['status'] = 'APPROVED'
            approved.append(prop)
            logger.info(f"Approved {side} {symbol}: {prop.get('reason', '')}")

        return approved

    def _get_last_order_times(self) -> Dict[str, datetime]:
        """Fetch recent orders to find last order time per symbol."""
        try:
            orders = trading_client.get_orders(status='all', limit=100)
            last_times = {}
            for order in orders:
                if order.symbol in last_times:
                    last_times[order.symbol] = max(last_times[order.symbol], order.submitted_at)
                else:
                    last_times[order.symbol] = order.submitted_at
            return last_times
        except Exception as e:
            logger.error(f"Error fetching orders: {e}")
            return {}

# ------------------------------------------------------------
# 4. EXECUTION AGENT
# ------------------------------------------------------------
class ExecutionAgent:
    """Submits approved orders to Alpaca paper trading."""
    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run

    def execute(self, approved_proposals: List[Dict]) -> List[Dict]:
        """Place market orders for each approved proposal."""
        results = []
        for prop in approved_proposals:
            if prop['side'] == 'BUY':
                # Calculate quantity from notional
                price = self._get_latest_price(prop['symbol'])
                if not price:
                    continue
                qty = round(prop.get('notional', ENTRY_NOTIONAL) / price, 4)
                order = self._submit_order(prop['symbol'], qty, 'buy')
                results.append({
                    'symbol': prop['symbol'],
                    'side': 'BUY',
                    'qty': qty,
                    'order_id': order.id if hasattr(order, 'id') else 'dry-run',
                    'status': 'submitted'
                })
            elif prop['side'] == 'SELL':
                qty = prop.get('quantity', 0)
                if qty <= 0:
                    continue
                order = self._submit_order(prop['symbol'], qty, 'sell')
                results.append({
                    'symbol': prop['symbol'],
                    'side': 'SELL',
                    'qty': qty,
                    'order_id': order.id if hasattr(order, 'id') else 'dry-run',
                    'status': 'submitted'
                })
        return results

    def _submit_order(self, symbol: str, qty: float, side: str):
        """Submit market order (or simulate if dry_run)."""
        if self.dry_run:
            logger.info(f"DRY RUN: would submit {side.upper()} {qty} {symbol}")
            class DummyOrder:
                id = 'dry-run'
            return DummyOrder()

        order_data = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY if side == 'buy' else OrderSide.SELL,
            time_in_force=TimeInForce.DAY
        )
        try:
            return trading_client.submit_order(order_data)
        except Exception as e:
            logger.error(f"Order submission failed for {symbol}: {e}")
            return None

    def _get_latest_price(self, symbol: str) -> float:
        """Get latest traded price."""
        request = StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute, limit=1)
        bars = data_client.get_stock_bars(request)
        df = bars.df
        return df['close'].iloc[-1]

# ------------------------------------------------------------
# 5. POSITION ANALYSIS AGENT
# ------------------------------------------------------------
class PositionAnalysisAgent:
    """Summarizes current positions, PnL, exposure."""
    def analyze(self) -> Dict:
        positions = trading_client.get_all_positions()
        account = trading_client.get_account()
        summary = {
            'equity': float(account.equity),
            'cash': float(account.cash),
            'buying_power': float(account.buying_power),
            'positions': [{
                'symbol': p.symbol,
                'qty': float(p.qty),
                'avg_entry_price': float(p.avg_entry_price),
                'current_price': float(p.current_price),
                'market_value': float(p.market_value),
                'unrealized_pl': float(p.unrealized_pl)
            } for p in positions],
            'total_market_value': sum(float(p.market_value or 0) for p in positions),
            'unrealized_pnl': sum(float(p.unrealized_pl or 0) for p in positions)
        }
        return summary

# ------------------------------------------------------------
# MAIN CYCLE
# ------------------------------------------------------------
def run_cycle():
    logger.info("===== New Trading Cycle =====")

    mda = MarketDataAgent(SYMBOLS)
    strategy = StrategyAgent(mda)
    risk = RiskOversightAgent()
    execution = ExecutionAgent(dry_run=DRY_RUN)
    position_agent = PositionAnalysisAgent()

    # 1. Generate proposals
    proposals = strategy.generate_proposals()
    logger.info(f"Proposals: {len(proposals)}")
    for p in proposals:
        logger.info(f"  {p['side']} {p['symbol']}: {p['reason']}")

    # 2. Risk review
    approved = risk.review(proposals)
    logger.info(f"Approved after risk: {len(approved)}")

    # 3. Execute
    if approved:
        execution_results = execution.execute(approved)
        for r in execution_results:
            logger.info(f"  Executed {r['side']} {r['symbol']} qty={r['qty']} order={r['order_id']}")
    else:
        logger.info("No orders executed.")

    # 4. Position summary
    summary = position_agent.analyze()
    logger.info(f"Account: Equity=${summary['equity']:.2f} Cash=${summary['cash']:.2f}")
    logger.info(f"Positions: {len(summary['positions'])}")
    for pos in summary['positions']:
        logger.info(f"  {pos['symbol']}: qty={pos['qty']} entry={pos['avg_entry_price']} curr={pos['current_price']} PnL={pos['unrealized_pl']:.2f}")

if __name__ == '__main__':
    run_cycle()
