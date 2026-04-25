#!/bin/bash
# ============================================================
# Push 0DTE Momentum Skew Rider to GitHub
# ============================================================
# Usage: bash scripts/push_to_github.sh YOUR_GITHUB_USERNAME
# ============================================================

set -e  # Exit on any error

USERNAME=${1:-"YOUR_USERNAME"}
REPO_NAME="0dte-momentum-skew-rider"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=================================================="
echo "  0DTE Momentum Skew Rider — GitHub Setup"
echo "=================================================="
echo "Username: $USERNAME"
echo "Repo: $REPO_NAME"
echo "Directory: $REPO_DIR"
echo ""

# ── Pre-flight checks ────────────────────────────────────────
if ! command -v git &> /dev/null; then
    echo "❌ git not found. Install git first."
    exit 1
fi

if ! command -v gh &> /dev/null; then
    echo "ℹ️  GitHub CLI not found. You'll create the repo manually."
    echo "   Visit: https://github.com/new"
    MANUAL_MODE=true
fi

cd "$REPO_DIR"

# ── Initialize git if needed ─────────────────────────────────
if [ ! -d ".git" ]; then
    echo "📁 Initializing git repository..."
    git init
    git branch -M main
fi

# ── Create .gitignore ────────────────────────────────────────
cat > .gitignore << 'EOF'
# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
build/
dist/
*.egg-info/
.eggs/

# Virtual environments
venv/
env/
.venv/

# Environment variables — NEVER commit these
.env
*.env
.env.local
.env.production

# Logs — never commit trade logs (PII + alpha)
logs/
*.log

# IDE
.idea/
.vscode/
*.swp

# OS
.DS_Store
Thumbs.db

# Strategy artifacts — don't commit model state
*.pkl
*.joblib
backtest_results/
paper_trade_results/

# Secrets
secrets/
api_keys.txt
credentials.json
EOF

# ── Create .env.example ──────────────────────────────────────
cat > .env.example << 'EOF'
# ============================================================
# Environment Variables — Copy to .env and fill in values
# NEVER commit .env to git
# ============================================================

# Broker credentials
BROKER=tastytrade         # tastytrade | ibkr
BROKER_API_KEY=your_api_key_here
BROKER_API_SECRET=your_api_secret_here
BROKER_ACCOUNT_ID=your_account_id

# Market data
POLYGON_API_KEY=your_polygon_key     # For options chain data
TRADIER_API_KEY=your_tradier_key     # Alternative data source

# Alerting
SLACK_WEBHOOK_URL=https://hooks.slack.com/...
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

# Monitoring
PROMETHEUS_PORT=9090

# Strategy settings (override config)
MAX_POSITION_CONTRACTS=1             # Start with 1!
DAILY_LOSS_LIMIT_PCT=0.01            # 1% max daily loss
PAPER_TRADING=true                   # ALWAYS start paper
EOF

# ── Stage all files ──────────────────────────────────────────
echo "📦 Staging files..."
git add -A
git status --short

echo ""
echo "📝 Creating initial commit..."
git commit -m "feat: Initial implementation of 0DTE Momentum Skew Rider

Strategy architecture:
- Multi-factor signal engine (IV skew + GEX + momentum)
- Jane Street-style pre-trade risk guardian (9 checks)
- Kelly-criterion position sizing with volatility adjustment
- Circuit breaker system (7 independent kill switches)
- Real-time Greeks monitoring with gamma sunset rules
- Smart limit order execution with aggression stepping
- Broker adapter pattern (IBKR, TastyTrade, Paper)

Risk management:
- Hard daily loss limit with kill switch
- Portfolio Greek budgets (delta, gamma, vega, charm)
- Gamma sunset rules (mandatory size reduction <90min to close)
- Consecutive loss halts with manual reset requirement
- Fat-finger protection and duplicate order prevention
- GEX-aware position management
- Correlation/concentration limits

See README.md for full documentation and setup instructions.

DISCLAIMER: For educational/research purposes only.
Not financial advice. Always paper trade first."

# ── Push to GitHub ───────────────────────────────────────────
if [ "$MANUAL_MODE" = true ]; then
    echo ""
    echo "=================================================="
    echo "  Manual GitHub Setup Required"
    echo "=================================================="
    echo ""
    echo "1. Create repo at: https://github.com/new"
    echo "   - Name: $REPO_NAME"
    echo "   - Private: YES (protect your alpha)"
    echo "   - Don't initialize with README"
    echo ""
    echo "2. Then run:"
    echo "   git remote add origin https://github.com/$USERNAME/$REPO_NAME.git"
    echo "   git push -u origin main"
    echo ""
else
    echo "🚀 Creating GitHub repository (PRIVATE)..."
    gh repo create "$REPO_NAME" \
        --private \
        --description "Institutional-grade 0DTE Momentum Skew Rider with HFT-class risk management" \
        --source=. \
        --remote=origin \
        --push

    echo ""
    echo "✅ Successfully pushed to GitHub!"
    echo "   https://github.com/$USERNAME/$REPO_NAME"
fi

echo ""
echo "=================================================="
echo "  Next Steps"
echo "=================================================="
echo "1. cp .env.example .env"
echo "2. Fill in your broker API credentials in .env"
echo "3. Implement broker adapter (src/execution/broker_adapters/)"
echo "4. Implement data feeds (_fetch_current_snapshot in skew_signal.py)"
echo "5. Run: python main.py --mode paper"
echo "6. Paper trade 60+ sessions before going live"
echo "7. Review logs daily — adjust parameters conservatively"
echo ""
echo "⚠️  NEVER skip paper trading. 0DTE gamma risk is extreme."
echo "=================================================="
