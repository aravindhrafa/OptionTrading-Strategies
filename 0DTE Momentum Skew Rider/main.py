"""
0DTE Momentum Skew Rider — Entry Point
=======================================

Usage:
    python main.py --mode paper          # Paper trading (ALWAYS start here)
    python main.py --mode live           # Live trading (after 60+ paper sessions)
    python main.py --mode backtest       # Run backtest
    python main.py --config custom.yaml  # Custom config file
"""

import asyncio
import argparse
import sys
from pathlib import Path

import structlog

# Setup logging first
log = structlog.get_logger(__name__)


def parse_args():
    parser = argparse.ArgumentParser(
        description="0DTE Momentum Skew Rider — Options Strategy Engine",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
IMPORTANT: Always start with paper trading mode.
Run at least 60 paper sessions before considering live trading.
Set conservative risk limits and review daily logs.

Risk Disclaimer: Options trading involves substantial risk of loss.
0DTE options in particular carry extreme gamma risk. Never trade
with capital you cannot afford to lose entirely.
        """
    )

    parser.add_argument(
        "--mode",
        choices=["paper", "live", "backtest"],
        default="paper",
        help="Trading mode (default: paper)"
    )

    parser.add_argument(
        "--config",
        default="config/base_config.yaml",
        help="Path to configuration file"
    )

    parser.add_argument(
        "--symbol",
        action="append",
        dest="symbols",
        help="Override universe symbols (can specify multiple)"
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate config and connections without trading"
    )

    return parser.parse_args()


async def main():
    args = parse_args()

    # ── Safety confirmation for live mode ───────────────────────
    if args.mode == "live":
        print("\n" + "="*60)
        print("⚠️  LIVE TRADING MODE")
        print("="*60)
        print("You are about to trade with REAL MONEY.")
        print("Ensure you have:")
        print("  ✓ Completed 60+ paper trading sessions")
        print("  ✓ Reviewed and understood all risk limits")
        print("  ✓ Funded account with capital you can afford to lose")
        print("  ✓ Set up monitoring and alerts")
        print("="*60)
        confirm = input("\nType 'I UNDERSTAND THE RISKS' to proceed: ")

        if confirm != "I UNDERSTAND THE RISKS":
            print("Confirmation failed. Exiting.")
            sys.exit(1)

    # ── Load and validate config ─────────────────────────────────
    config_path = args.config
    if not Path(config_path).exists():
        log.error("config.not_found", path=config_path)
        sys.exit(1)

    # ── Initialize strategy ──────────────────────────────────────
    from src.core.strategy import ZeroDTEMomentumSkewRider

    log.info("startup.initializing",
             mode=args.mode,
             config=config_path)

    strategy = ZeroDTEMomentumSkewRider(config_path=config_path)

    # Override mode from CLI
    strategy.config.strategy.mode = args.mode

    # Override symbols if specified
    if args.symbols:
        strategy.config.strategy.universe = args.symbols
        log.info("startup.symbol_override", symbols=args.symbols)

    if args.dry_run:
        print("Dry run: Configuration and connections validated. Exiting.")
        sys.exit(0)

    # ── Run ──────────────────────────────────────────────────────
    log.info("startup.running", mode=args.mode)
    await strategy.run()


if __name__ == "__main__":
    asyncio.run(main())
