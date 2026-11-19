#!/bin/bash
# Setup script for Game of War Jewel Claim automation

echo "=== Game of War - Jewel Claim Setup ==="
echo ""

# Check Python version
echo "Checking Python installation..."
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is not installed. Please install Python 3.8 or higher."
    exit 1
fi

PYTHON_VERSION=$(python3 --version | cut -d' ' -f2)
echo "✓ Python $PYTHON_VERSION found"
echo ""

# Install requirements
echo "Installing required packages..."
pip3 install -r requirements.txt

echo ""
echo "Installing Playwright browsers..."
python3 -m playwright install chromium

echo ""
echo "=== Setup Complete! ==="
echo ""
echo "Test the script now:"
echo "  python3 claim_jewels_browser.py"
echo ""
echo "Set up automation (runs daily at 1 AM):"
echo "  1. Run: crontab -e"
echo "  2. Add: 0 1 * * * cd /Users/johnalvero/Apps/godofwar && /usr/bin/python3 claim_jewels_browser.py >> jewel_claim.log 2>&1"
echo "  3. Save and exit"
echo ""
