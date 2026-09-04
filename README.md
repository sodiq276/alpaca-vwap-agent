# VWAP Mean-Reversion Trading Agent (Alpaca Paper Trading)

An autonomous intraday trading agent that implements a **VWAP mean-reversion** strategy on liquid US stocks. Built for the Alpaca AI Trading Agents Hackathon. Paper trading only.

## Strategy Highlights
- Universe: AAPL, MSFT, NVDA, AMD, TSLA, META, AMZN
- Entry: price at least 0.5% below session VWAP AND volume ratio ≥ 1.2
- Exit: price returns within 0.1% of VWAP OR loss ≥ 0.5%
- Risk: max 4 positions, $500 total exposure, 10-minute cooldown
- Orders: simple market orders only, no bracket orders
- Data: 1-minute bars, IEX feed (free for paper)

## Architecture
Five agents orchestrated via prompt injection in Claude Desktop with Alpaca MCP:

1. Market Data Agent – data and indicators
2. Strategy Agent – BUY/SELL/HOLD proposals
3. Risk Oversight Agent – veto authority
4. Execution Agent – paper order submission
5. Position Analysis Agent – PnL and exit monitoring

## How to Run
1. Configure Alpaca paper API keys in `.env`:
2. Install dependencies:
    bash
    pip install alpaca-py pandas numpy python-dotenv
3. Run the agent:
     python vwap_agent.py 
## DEMO
Watch the demo video: [insert link]
