import os
import time
import signal
import logging
import pandas as pd
import numpy as np
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional

from dotenv import load_dotenv
load_dotenv()

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# ------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------
API_KEY = os.getenv('ALPACA_KEY')
SECRET_KEY = os.getenv('ALPACA_SECRET')

SYMBOLS = ['AAPL', 'MSFT', 'NVDA', 'AMD', 'TSLA', 'META', 'AMZN']
MAX_POSITIONS = 4
MAX_TOTAL_EXPOSURE = 500.0
ENTRY_NOTIONAL = 100.0
COOLDOWN_SECONDS = 600
VWAP_ENTRY_DISTANCE = 0.005
VWAP_EXIT_DISTANCE = 0.001
VOLUME_RATIO_THRESHOLD = 1.2
LOSS_EXIT_PERCENT = -0.005
POLL_INTERVAL_SECONDS = 60  # How often the agent runs its cycle

PAPER_TRADING = True
DRY_RUN = False
# ------------------------------------------------------------
# LOGGING SETUP
# ------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('agent_pro.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('ProTradingAgent')

# ------------------------------------------------------------
# ALPACA CLIENTS
# ------------------------------------------------------------
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER_TRADING)

# ------------------------------------------------------------
# 1. MARKET DATA AGENT
# ------------------------------------------------------------
class MarketDataAgent:
    def __init__(self, symbols: List[str]):
        self.symbols = symbols

    def get_bars(self, symbol: str, limit: int = 200) -> Optional[pd.DataFrame]:
        try:
            now = datetime.now(timezone.utc) - timedelta(minutes=2)
            request = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Minute,
                start=now - timedelta(minutes=limit),
                end=now,
                limit=limit,
                feed=DataFeed.IEX
            )
            bars = data_client.get_stock_bars(request)
            df = bars.df

            if df is None or df.empty:
                return None

            if isinstance(df.index, pd.MultiIndex):
                df = df.reset_index(level='symbol', drop=True)

            if not isinstance(df.index, pd.DatetimeIndex):
                if 'timestamp' in df.columns:
                    df['timestamp'] = pd.to_datetime(df['timestamp'])
                    df = df.set_index('timestamp')
                else:
                    df.index = pd.to_datetime(df.index)

            if len(df) < 20:
                return None

            return df
        except Exception as e:
            logger.error(f"Data fetch error for {symbol}: {e}")
            return None

    def compute_indicators(self, df: pd.DataFrame, symbol: str) -> Optional[Dict]:
        if df is None or df.empty or len(df) < 20:
            return None

        typical_price = (df['high'] + df['low'] + df['close']) / 3
        cum_vol = df['volume'].cumsum()
        
        if cum_vol.iloc[-1] == 0:
            return None

        vwap_series = (typical_price * df['volume']).cumsum() / cum_vol
        current_vwap = vwap_series.iloc[-1]
        last_price = df['close'].iloc[-1]
        distance_from_vwap = (last_price - current_vwap) / current_vwap

        rolling_vol_20 = df['volume'].rolling(20).mean().iloc[-1]
        current_volume = df['volume'].iloc[-1]
        volume_ratio = current_volume / rolling_vol_20 if rolling_vol_20 and rolling_vol_20 > 0 else 0

        return {
            'symbol': symbol,
            'last_price': last_price,
            'vwap': current_vwap,
            'distance_from_vwap': distance_from_vwap,
            'volume_ratio': volume_ratio,
        }

# ------------------------------------------------------------
# 2. STRATEGY AGENT
# ------------------------------------------------------------
class StrategyAgent:
    def __init__(self, mda: MarketDataAgent):
        self.mda = mda

    def generate_proposals(self, positions: List) -> List[Dict]:
        proposals = []
        position_symbols = {p.symbol: p for p in positions}

        for symbol in SYMBOLS:
            bars = self.mda.get_bars(symbol)
            indicators = self.mda.compute_indicators(bars, symbol)
            if not indicators:
                continue

            if symbol in position_symbols:
                pos = position_symbols[symbol]
                entry_price = float(pos.avg_entry_price)
                current_price = indicators['last_price']

                if indicators['distance_from_vwap'] >= -VWAP_EXIT_DISTANCE:
                    proposals.append({
                        'symbol': symbol,
                        'side': 'SELL',
                        'reason': 'Take Profit: Price near VWAP',
                        'quantity': float(pos.qty)
                    })
                    continue

                loss_pct = (current_price - entry_price) / entry_price
                if loss_pct <= LOSS_EXIT_PERCENT:
                    proposals.append({
                        'symbol': symbol,
                        'side': 'SELL',
                        'reason': f'Stop Loss triggered ({loss_pct:.2%})',
                        'quantity': float(pos.qty)
                    })
                continue

            if len(positions) >= MAX_POSITIONS:
                continue

            if (indicators['distance_from_vwap'] <= -VWAP_ENTRY_DISTANCE and
                indicators['volume_ratio'] >= VOLUME_RATIO_THRESHOLD):
                proposals.append({
                    'symbol': symbol,
                    'side': 'BUY',
                    'reason': f"VWAP Dist: {indicators['distance_from_vwap']:.2%}, Vol Ratio: {indicators['volume_ratio']:.2f}",
                    'notional': ENTRY_NOTIONAL
                })
        return proposals

# ------------------------------------------------------------
# 3. RISK OVERSIGHT AGENT
# ------------------------------------------------------------
class RiskOversightAgent:
    def __init__(self):
        self.max_positions = MAX_POSITIONS
        self.max_exposure = MAX_TOTAL_EXPOSURE
        self.cooldown = COOLDOWN_SECONDS

    def review(self, proposals: List[Dict], positions: List) -> List[Dict]:
        approved = []
        current_positions = len(positions)
        total_market_value = sum(float(p.market_value or 0) for p in positions)
        last_order_times = self._get_last_order_times()

        for prop in proposals:
            symbol = prop['symbol']
            side = prop['side']

            if side == 'BUY':
                if current_positions >= self.max_positions:
                    logger.warning(f"Risk Reject -> BUY {symbol}: Max positions ({self.max_positions}) reached.")
                    continue
                if total_market_value + prop.get('notional', 0) > self.max_exposure:
                    logger.warning(f"Risk Reject -> BUY {symbol}: Exposure limit exceeded.")
                    continue
                if symbol in last_order_times:
                    last_time = last_order_times[symbol]
                    if (datetime.now(timezone.utc) - last_time).total_seconds() < self.cooldown:
                        logger.warning(f"Risk Reject -> BUY {symbol}: Cooldown active.")
                        continue

            prop['status'] = 'APPROVED'
            approved.append(prop)
            logger.info(f"Risk Approve -> {side} {symbol}: {prop.get('reason', '')}")

        return approved

    def _get_last_order_times(self) -> Dict[str, datetime]:
        try:
            orders = trading_client.get_orders()
            last_times = {}
            for order in orders:
                if order.submitted_at:
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
    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run

    def execute(self, approved_proposals: List[Dict]) -> List[Dict]:
        results = []
        for prop in approved_proposals:
            try:
                if prop['side'] == 'BUY':
                    order = self._submit_order(prop['symbol'], notional=prop.get('notional', ENTRY_NOTIONAL), side='buy')
                    results.append({'symbol': prop['symbol'], 'side': 'BUY', 'status': 'submitted', 'order_id': getattr(order, 'id', 'dry-run')})
                elif prop['side'] == 'SELL':
                    qty = prop.get('quantity', 0)
                    if qty > 0:
                        order = self._submit_order(prop['symbol'], qty=qty, side='sell')
                        results.append({'symbol': prop['symbol'], 'side': 'SELL', 'status': 'submitted', 'order_id': getattr(order, 'id', 'dry-run')})
            except Exception as e:
                logger.error(f"Execution failure for {prop['symbol']}: {e}")
        return results

    def _submit_order(self, symbol: str, notional: float = None, qty: float = None, side: str = 'buy'):
        if self.dry_run:
            logger.info(f"[DRY RUN] Would submit {side.upper()} order for {symbol}")
            class DummyOrder: id = 'dry-run'
            return DummyOrder()

        order_data = MarketOrderRequest(
            symbol=symbol,
            notional=notional,
            qty=qty if not notional else None,
            side=OrderSide.BUY if side == 'buy' else OrderSide.SELL,
            time_in_force=TimeInForce.DAY
        )
        return trading_client.submit_order(order_data)

# ------------------------------------------------------------
# 5. CORE SYSTEM LOOP
# ------------------------------------------------------------
def get_managed_positions() -> List:
    try:
        return [p for p in trading_client.get_all_positions() if p.symbol in SYMBOLS]
    except Exception as e:
        logger.error(f"Failed to fetch positions: {e}")
        return []

def run_cycle():
    logger.info("--- Starting Trading Cycle ---")
    
    positions = get_managed_positions()
    mda = MarketDataAgent(SYMBOLS)
    strategy = StrategyAgent(mda)
    risk = RiskOversightAgent()
    execution = ExecutionAgent(dry_run=DRY_RUN)

    # 1. Strategy Generation
    proposals = strategy.generate_proposals(positions)
    
    # 2. Risk Review
    approved = risk.review(proposals, positions)
    
    # 3. Execution
    if approved:
        results = execution.execute(approved)
        for r in results:
            logger.info(f"Executed: {r['side']} {r['symbol']} | Order ID: {r['order_id']}")
    else:
        logger.info("No actionable signals generated.")

    # 4. Status Update
    if positions:
        logger.info(f"Open Managed Positions: {len(positions)}")
        for p in positions:
            logger.info(f"  -> {p.symbol}: Qty {p.qty} | PnL: ${float(p.unrealized_pl):.2f}")
    
    logger.info("--- Cycle Complete ---")

# Graceful exit handler
run_bot = True
def handle_exit(signum, frame):
    global run_bot
    logger.info("\nTermination signal received. Shutting down gracefully...")
    run_bot = False

if __name__ == '__main__':
    signal.signal(signal.SIGINT, handle_exit)
    signal.signal(signal.SIGTERM, handle_exit)

    logger.info("Starting Pro Trading Agent Daemon...")
    logger.info(f"Dry Run Mode: {DRY_RUN}")
    
    while run_bot:
        try:
            # Check market status before burning API limits
            clock = trading_client.get_clock()
            if not clock.is_open:
                time_to_open = clock.next_open - datetime.now(timezone.utc)
                logger.info(f"Market is closed. Next open in {time_to_open}. Sleeping for 5 minutes...")
                time.sleep(300)
                continue
                
            run_cycle()
            time.sleep(POLL_INTERVAL_SECONDS)
            
        except Exception as e:
            logger.error(f"System Error in main loop: {e}", exc_info=True)
            time.sleep(POLL_INTERVAL_SECONDS)
