# Multi-Agent Momentum Breakout Agent (Alpaca Paper Trading)

An original AI trading agent using a custom momentum breakout strategy with ATR-based risk management. Built for the Alpaca AI Trading Agents Hackathon.

## Strategy Highlights
- Universe: 10 liquid US stocks
- Entry: price breaks above 20-minute high, volume surge, positive sentiment
- Exit: breakdown below 20-minute low, time stop, loss stop, or ATR profit target
- Risk: max 4 positions, $500 total exposure, 10-minute cooldown
- Paper trading only, simple market orders

## Architecture
Five agents orchestrated via prompt injection in Claude Desktop with Alpaca MCP:
1. Market Data Agent – data and indicators
2. Strategy Agent – BUY/SELL/HOLD proposals
3. Risk Oversight Agent – veto authority
4. Execution Agent – paper order submission
5. Position Analysis Agent – PnL and exit monitoring

## How to Run
1. Configure Alpaca paper API keys in `.env`
2. Connect Claude Desktop to Alpaca MCP
3. Paste the prompts from `prompts.md`
4. Run a cycle

## Demo
Watch the demo video: [insert link]

## Disclaimer
Paper trading only. Not financial advice.
