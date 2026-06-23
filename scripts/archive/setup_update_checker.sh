#!/bin/bash
# Drewgent Update Checker - Setup Script
# Run this to install the update checker cron job

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DREWENT_HOME="${DREWENT_HOME:-$HOME/.drewgent}"
DREWENT_AGENT="$DREWENT_HOME/drewgent-agent"
SCRIPT_TARGET="$DREWENT_HOME/scripts/check_drewgent_update.py"

echo "🔧 Setting up Drewgent Update Checker..."

# Create scripts directory if not exists
mkdir -p "$DREWENT_HOME/scripts"

# Check if drewgent-agent exists
if [ -d "$DREWENT_AGENT" ]; then
    # Use the actual drewgent-agent install
    SOURCE_DIR="$DREWENT_AGENT"
else
    # Fall back to current directory (dev install)
    SOURCE_DIR="$SCRIPT_DIR"
fi

# Copy the update checker script
if [ -f "$SOURCE_DIR/check_drewgent_update.py" ]; then
    cp "$SOURCE_DIR/check_drewgent_update.py" "$SCRIPT_TARGET"
    echo "✅ Installed update checker to $SCRIPT_TARGET"
else
    echo "❌ Source script not found at $SOURCE_DIR/check_drewgent_update.py"
    exit 1
fi

# Check if cron tool is available
if command -v drewgent &> /dev/null; then
    # Check if job already exists
    if drewgent cron list 2>/dev/null | grep -q "Drewgent Update"; then
        echo "ℹ️  Update checker cron job already exists"
    else
        # Add cron job (every 6 hours)
        echo "📅 Adding cron job (every 6 hours)..."
        drewgent cron add "every 6h" "Check Drewgent updates" \
            --script "$SCRIPT_TARGET" \
            --skill drewgent 2>/dev/null || \
        drewgent cron add "every 6h" "Check Drewgent updates" \
            --script "$SCRIPT_TARGET" 2>/dev/null || \
        echo "⚠️  Could not auto-add cron job. Run manually:"
        echo "    drewgent cron add \"every 6h\" \"Check Drewgent updates\" --script $SCRIPT_TARGET"
    fi
else
    echo "⚠️  Drewgent CLI not found in PATH. Manual cron setup required:"
    echo "    drewgent cron add \"every 6h\" \"Check Drewgent updates\" --script $SCRIPT_TARGET"
fi

# Check for Discord webhook
if [ -z "$DISCORD_WEBHOOK_URL" ] && [ ! -f "$DREWENT_HOME/.env" ]; then
    echo ""
    echo "💡 To enable Discord notifications, add to $DREWENT_HOME/.env:"
    echo '   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/your/webhook'
elif [ -n "$DISCORD_WEBHOOK_URL" ]; then
    echo "✅ Discord webhook configured"
fi

echo ""
echo "✅ Setup complete!"
echo ""
echo "Usage:"
echo "  python3 $SCRIPT_TARGET --status    # Check status"
echo "  python3 $SCRIPT_TARGET --notify    # Check and notify via Discord"
echo "  python3 $SCRIPT_TARGET --autopull # Check and auto-pull"
echo ""
echo "Status file: $DREWENT_HOME/update_status.json"
