# 🎮 Game of War - Automated Multi-Account Jewel Claiming System

Automated browser-based system for claiming free daily jewels from Game of War accounts. Supports **multi-character accounts** and **batch processing**.

---

## ✨ Key Features

- ✅ **Multi-Character Support**: Automatically detects and claims jewels for ALL characters on each account
- ✅ **Batch Processing**: Process all accounts or specific ranges
- ✅ **Anti-Bot Detection Bypass**: Uses real browser automation (Playwright) to avoid detection
- ✅ **Detailed Logging**: Timestamped logs with success/failure tracking
- ✅ **Screenshot Capture**: Saves screenshots for verification (optional)
- ✅ **Rate Limiting**: 10-second delays between accounts to avoid triggering rate limits
- ✅ **Robust Error Handling**: Gracefully handles timeouts, missing elements, and network issues
- ✅ **Flexible Scheduling**: Run all accounts or specific batches on custom schedules

---

## 🚀 Quick Start

```bash
# 1. Navigate to project directory
cd godofwar

# 2. Activate virtual environment
source .venv/bin/activate

# 3. Test with a single account
python claim_all_characters.py 1 1

# 4. Process all 344 accounts
python claim_all_characters.py
```

---

## 📦 Installation & Setup

### Requirements

- Python 3.8+
- Virtual environment (`.venv`)
- Playwright browser automation library
- Chromium browser (installed via Playwright)

### Installation Steps

1. **Clone/download the project**
   ```bash
   cd godofwar
   ```

2. **Activate virtual environment**
   ```bash
   source .venv/bin/activate
   ```

3. **Install dependencies** (if not already installed)
   ```bash
   pip install -r requirements.txt
   playwright install chromium
   ```

---

## 📁 File Structure

```
godofwar/
├── users.txt                    # Account credentials (email:password format)
├── claim_all_characters.py      # ⭐ Main script - Multi-character support
├── screenshots/                 # Screenshot storage (auto-created)
├── logs/                        # Cron job logs (create if using cron)
└── README.md                    # This file
```

---

## 🔧 Account Configuration

### users.txt Format

One account per line in the format `email:password`:

```
user321@example.com:mypassword
user123@example.com:mypassword
user456@yahoo.com:mypassword
```

**Notes:**
- Blank lines and lines starting with `#` are ignored
- Keep this file secure (contains plaintext passwords)

### Adding New Accounts

Simply append to `users.txt`:

```bash
echo "newemail@example.com:newpassword" >> users.txt
```

---

## 💻 Usage

### 1. Process All Accounts

Run without parameters to process all accounts:

```bash
source .venv/bin/activate
python claim_all_characters.py
```

**Expected runtime**: 30-40 seconds per account

### 2. Process Specific Range

Process a subset of accounts by specifying line numbers:

```bash
# Process lines 1-50
python claim_all_characters.py 1 50

# Process lines 51-100
python claim_all_characters.py 51 100

# Process single account (line 325)
python claim_all_characters.py 325 325
```

### 3. Recommended Batch Strategy

Split large accounts into batches to avoid long single runs:

```bash
# Batch 1: Lines 1-50 (50 accounts)
python claim_all_characters.py 1 50

# Batch 2: Lines 51-100 (50 accounts)
python claim_all_characters.py 51 100
```

---

## ⏰ Automation with Cron Jobs

### Jewel Reset Time

- **Reset time**: 00:00 UTC (8:00 AM Philippines time)
- **Recommended run time**: 01:00 UTC (9:00 AM Philippines time) - 1 hour after reset

### Setting Up Cron Jobs

Create logs directory:
```bash
mkdir -p godofwar/logs
```

Edit your crontab:
```bash
crontab -e
```

Add the following schedules (7 batches from 1 AM - 7 AM UTC):

```bash
# Game of War Jewel Claiming - Batch 1 (Lines 1-50)
0 1 * * * cd /Users/<user>/godofwar && source .venv/bin/activate && python claim_all_characters.py 1 50 >> /Users/johnalvero/godofwar/logs/batch1.log 2>&1

# Game of War Jewel Claiming - Batch 2 (Lines 51-100)
0 2 * * * cd /Users/<user>/godofwar && source .venv/bin/activate && python claim_all_characters.py 51 100 >> /Users/johnalvero/godofwar/logs/batch2.log 2>&1
```

### Verify Cron Jobs

```bash
# List all cron jobs
crontab -l

# Check logs
tail -f /Users/johnalvero/godofwar/logs/batch1.log
```

---

## 🔍 How It Works

### Multi-Character Flow

1. **Login**
   - Navigate to login page
   - Fill credentials
   - Dismiss cookie consent (OneTrust)
   - Click login button

2. **Character Detection**
   - Detect all available characters in character selection dialog
   - Log total characters found (e.g., "🎮 Found 2 characters!")

3. **Character Loop** (for each character):
   - Select the character
   - Navigate to vault (`/catalog/main`)
   - Check for "Claim Now!" button
   - Click claim button → 75 jewels
   - Verify success ("Available again at" message)
   - **If not last character:**
     - Click avatar button (top right)
     - Click "Change Account" from dropdown
     - Select next character

4. **Summary**
   - Log total characters processed
   - Log total jewels claimed
   - Display success/failure status

### Key Technical Details

- **Headless mode**: Browser runs in background (`headless=True`)
- **Cookie consent handling**: Automatically dismisses OneTrust cookie dialogs
- **Character switching**: Avatar dropdown → "Change Account" → character selection
- **Verification**: Checks page content for "Available again at" to confirm claim
- **Error recovery**: Continues processing remaining accounts even if one fails
- **Rate limiting**: 10-second delay between accounts

---

### Status Indicators

- ✅ **Claimed successfully**: Jewels were claimed this run
- ✓ **Already claimed**: Jewels were already claimed today (on cooldown)
- ⚠ **Warning**: Non-critical issue (e.g., unclear claim status)
- ❌ **Failed**: Account processing failed completely
- 🎮 **Found N characters**: Detected multi-character account
- 💎 **Total jewels claimed**: Summary of jewels earned

---

## 🛠 Troubleshooting

### Common Issues

**1. "ModuleNotFoundError: No module named 'playwright'"**
```bash
source .venv/bin/activate
pip install playwright
playwright install chromium
```

**2. "Users file not found"**
- Verify `users.txt` exists in `/Users/<user>/godofwar/`
- Check file permissions

**3. "Timeout" errors**
- Increase timeouts in `claim_all_characters.py` line 67 (change `timeout=60000` to `timeout=120000`)
- Check internet connection
- Game of War website might be slow or down

**4. Cookie consent blocking**
- Already handled automatically (lines 133-145)
- If persists, verify OneTrust selectors haven't changed

**5. Character switching fails**
- Verify "Change Account" button exists (might vary by account type)
- Check `claim_all_characters.py` line 272 for selector
- Try debug mode to see what's happening

**6. False "Already claimed" status**
- Fixed in v2.0 - script now clicks claim button FIRST, then verifies
- See lines 71-95 for logic

**7. Only claiming first character**
- Make sure you're using `claim_all_characters.py` (not legacy scripts)
- Check that character switching logic executes (lines 261-288)

### Debug Mode

To run with visible browser for debugging:

1. Edit `claim_all_characters.py` line 110
2. Change `headless=True` to `headless=False`
3. Run script - browser window will be visible
4. Watch the automation in real-time

### Logs Location

- **Console output**: Shown in terminal
- **Cron logs**: `/Users/johnalvero/godofwar/logs/batch*.log`
- **Screenshots**: `/Users/johnalvero/godofwar/screenshots/` (if enabled)

---

## 🔒 Security Notes

- ⚠️ **Keep users.txt secure**: Contains plaintext passwords
- ⚠️ **Do not commit users.txt to Git**: Should be in `.gitignore`
- ⚠️ **Backup users.txt regularly**: Lost credentials cannot be recovered
- ⚠️ **Restrict file permissions**:
  ```bash
  chmod 600 /Users/johnalvero/godofwar/users.txt
  ```

### Optional: Use Environment Variables

For better security, consider using environment variables:

```bash
# Set environment variables
export GOW_USERS_FILE="/Users/johnalvero/godofwar/users.txt"

# Modify script to use:
# USERS_FILE = os.getenv("GOW_USERS_FILE", "/Users/johnalvero/godofwar/users.txt")
```

---

## 📝 Version History

### v2.0 (2025-12-25) - Multi-Character Support
- ✅ Automatically detects and claims for all characters per account
- ✅ Character switching via "Change Account" dropdown
- ✅ Improved error handling and logging
- ✅ Support for running all accounts without parameters
- ✅ Enhanced character detection (works in dialog or page)

### v1.1 (2025-12-24) - Bug Fixes
- ✅ Fixed false "already claimed" detection
- ✅ Cookie consent handling
- ✅ Improved verification logic

### v1.0 (2025-12-24) - Initial Release
- ✅ Single character claiming only
- ✅ Batch processing support
- ✅ Basic error handling

---

## 📋 Command Reference

```bash
# Show help
python claim_all_characters.py --help

# Process all accounts
python claim_all_characters.py

# Process specific range
python claim_all_characters.py <start_line> <end_line>

# Examples
python claim_all_characters.py 1 50        # First 50 accounts
python claim_all_characters.py 51 100      # Accounts 51-100
python claim_all_characters.py 325 325     # Single account (line 325)

# Check which account is on which line
grep -n "email@example.com" users.txt
```

---

## 🎯 Quick Start Summary

```bash
# 1. Navigate to project
cd /Users/<user>/godofwar

# 2. Activate environment
source .venv/bin/activate

# 3. Test with one account
python claim_all_characters.py 1 1

# 4. Process all accounts
python claim_all_characters.py

# 5. Set up automation (optional)
crontab -e
# Add: 0 1 * * * cd /Users/<user>/godofwar && source .venv/bin/activate && python claim_all_characters.py >> /Users/<user>/godofwar/logs/daily.log 2>&1

# 6. Monitor logs
tail -f /Users/<user>/godofwar/logs/daily.log
```

---



## 🏆 What This Achieves

1. 🤖 **Runs daily** at scheduled times (via cron)
2. 🎮 **Opens Game of War** in headless browser
3. 🔐 **Logs in** with credentials from users.txt
4. 🎯 **Detects all characters** on each account
5. 💎 **Claims 75 jewels per character** automatically
6. 📊 **Logs detailed results** to file
7. 🔄 **Repeats for all accounts** every day
8. ✅ **Set it and forget it!** 🚀

---

## 💎 Daily Earnings Potential

| Account Type | Characters | Jewels/Day | Monthly |
|--------------|-----------|------------|---------|
| Single char | 1 | 75 | 2,250 |
| Multi char | 2 | 150 | 4,500 |
| Multi char | 3 | 225 | 6,750 |

**344 accounts × 75 jewels = 25,800 jewels/day minimum**

**With multi-character accounts: 30,000-40,000+ jewels/day possible!**

---

**Enjoy your automated daily jewels!** 💎✨

**Last Updated**: December 25, 2025 | **Total Accounts**: 344 | **Version**: 2.0
