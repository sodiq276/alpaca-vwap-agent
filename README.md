# Alpaca VWAP Mean-Reversion Agent (Prompt-Only Workflow)

This project demonstrates an agentic trading workflow using **Alpaca's Trading API and MCP server**. No manual Python code was written for the strategy; the entire system is driven by prompt injection into Claude Desktop connected to Alpaca through MCP.

## Architecture
Five agents handle the workflow:
1. Market Data Agent – fetches 1-minute bars and computes indicators.
2. Strategy Agent – converts indicators into BUY/SELL/HOLD proposals.
3. Risk Oversight Agent – approves/rejects proposals with veto authority.
4. Execution Agent – submits approved simple market orders to Alpaca paper trading.
5. Position Analysis Agent – summarizes positions, PnL, exposure.

## Strategy
- Universe: SPY, QQQ, IWM, AAPL, MSFT, NVDA, AMD, TSLA, META, AMZN
- Entry: buy $100 notional when price at least 0.5% below VWAP and volume ≥ 1.2× rolling 20-minute average.
- Exit: sell when price within 0.1% of VWAP, or holding time >15 minutes, or loss >0.5%.
- Risk limits: max 4 positions, max $500 exposure, 10-minute cooldown per symbol.

## How to run
1. Set up Alpaca paper trading API keys.
2. Connect Claude Desktop to Alpaca via MCP.
3. Paste the prompts from `prompts.md` into Claude.
4. Run a cycle and observe the output.

## Demo
Watch the demo video: [link-to-video]

## Disclaimer
Paper trading only. Not financial advice.
