# 🎮 Game of War - Automated Multi-Account Jewel Claiming System

Automated browser-based system for claiming free daily jewels (75 per character) from Game of War accounts. Supports **multi-character accounts** and **batch processing** for up to 344 accounts.

---

## ✨ Key Features

- ✅ **Multi-Character Support**: Automatically detects and claims jewels for ALL characters on each account
- ✅ **Batch Processing**: Process all 344 accounts or specific ranges
- ✅ **Anti-Bot Detection Bypass**: Uses real browser automation (Playwright) to avoid detection
- ✅ **Detailed Logging**: Timestamped logs with success/failure tracking
- ✅ **Screenshot Capture**: Saves screenshots for verification (optional)
- ✅ **Rate Limiting**: 10-second delays between accounts to avoid triggering rate limits
- ✅ **Robust Error Handling**: Gracefully handles timeouts, missing elements, and network issues
- ✅ **Flexible Scheduling**: Run all accounts or specific batches on custom schedules

---

## 📊 Expected Earnings

### Current Setup (344 Accounts)

**Single-Character Accounts:**
- Per account: **75 jewels/day**
- 344 accounts: **25,800 jewels/day**
- Monthly: **~774,000 jewels**

**Multi-Character Accounts:**
- Per character: **75 jewels/day**
- Example: 2 characters = **150 jewels/day**
- Potential: **50-100% more jewels** if many accounts have multiple characters

---

## 🚀 Quick Start

```bash
# 1. Navigate to project directory
cd /Users/johnalvero/godofwar

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
   cd /Users/johnalvero/godofwar
   ```

2. **Activate virtual environment**
   ```bash
   source .venv/bin/activate
   ```

3. **Install dependencies** (if not already installed)
   ```bash
   pip install playwright
   playwright install chromium
   ```

---

## 📁 File Structure

```
godofwar/
├── users.txt                    # Account credentials (email:password format)
├── claim_all_characters.py      # ⭐ Main script - Multi-character support
├── claim_batch.py               # Legacy - Single character only
├── claim_all.py                 # Legacy - Single character only
├── screenshots/                 # Screenshot storage (auto-created)
├── logs/                        # Cron job logs (create if using cron)
├── farms.csv                    # Original CSV of accounts
└── README.md                    # This file
```

---

## 🔧 Account Configuration

### users.txt Format

One account per line in the format `email:password`:

```
webero7246@plexfirm.com:6621abcd
user123@example.com:password123
meunknown374@yahoo.com:popcorn1993
```

**Notes:**
- **Total accounts**: 344
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

Run without parameters to process all 344 accounts:

```bash
source .venv/bin/activate
python claim_all_characters.py
```

**Expected runtime**: ~3-4 hours for all 344 accounts (30-40 seconds per account)

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

Split 344 accounts into 7 batches to avoid long single runs:

```bash
# Batch 1: Lines 1-50 (50 accounts)
python claim_all_characters.py 1 50

# Batch 2: Lines 51-100 (50 accounts)
python claim_all_characters.py 51 100

# Batch 3: Lines 101-150 (50 accounts)
python claim_all_characters.py 101 150

# Batch 4: Lines 151-200 (50 accounts)
python claim_all_characters.py 151 200

# Batch 5: Lines 201-250 (50 accounts)
python claim_all_characters.py 201 250

# Batch 6: Lines 251-300 (50 accounts)
python claim_all_characters.py 251 300

# Batch 7: Lines 301-344 (44 accounts)
python claim_all_characters.py 301 344
```

---

## ⏰ Automation with Cron Jobs

### Jewel Reset Time

- **Reset time**: 00:00 UTC (8:00 AM Philippines time)
- **Recommended run time**: 01:00 UTC (9:00 AM Philippines time) - 1 hour after reset

### Setting Up Cron Jobs

Create logs directory:
```bash
mkdir -p /Users/johnalvero/godofwar/logs
```

Edit your crontab:
```bash
crontab -e
```

Add the following schedules (7 batches from 1 AM - 7 AM UTC):

```bash
# Game of War Jewel Claiming - Batch 1 (Lines 1-50)
0 1 * * * cd /Users/johnalvero/godofwar && source .venv/bin/activate && python claim_all_characters.py 1 50 >> /Users/johnalvero/godofwar/logs/batch1.log 2>&1

# Game of War Jewel Claiming - Batch 2 (Lines 51-100)
0 2 * * * cd /Users/johnalvero/godofwar && source .venv/bin/activate && python claim_all_characters.py 51 100 >> /Users/johnalvero/godofwar/logs/batch2.log 2>&1

# Game of War Jewel Claiming - Batch 3 (Lines 101-150)
0 3 * * * cd /Users/johnalvero/godofwar && source .venv/bin/activate && python claim_all_characters.py 101 150 >> /Users/johnalvero/godofwar/logs/batch3.log 2>&1

# Game of War Jewel Claiming - Batch 4 (Lines 151-200)
0 4 * * * cd /Users/johnalvero/godofwar && source .venv/bin/activate && python claim_all_characters.py 151 200 >> /Users/johnalvero/godofwar/logs/batch4.log 2>&1

# Game of War Jewel Claiming - Batch 5 (Lines 201-250)
0 5 * * * cd /Users/johnalvero/godofwar && source .venv/bin/activate && python claim_all_characters.py 201 250 >> /Users/johnalvero/godofwar/logs/batch5.log 2>&1

# Game of War Jewel Claiming - Batch 6 (Lines 251-300)
0 6 * * * cd /Users/johnalvero/godofwar && source .venv/bin/activate && python claim_all_characters.py 251 300 >> /Users/johnalvero/godofwar/logs/batch6.log 2>&1

# Game of War Jewel Claiming - Batch 7 (Lines 301-344)
0 7 * * * cd /Users/johnalvero/godofwar && source .venv/bin/activate && python claim_all_characters.py 301 344 >> /Users/johnalvero/godofwar/logs/batch7.log 2>&1
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

## 📈 Output Format

### Console Output Example

```
[2025-12-25 21:01:13] [INFO] ============================================================
[2025-12-25 21:01:13] [INFO] Game of War - Multi-Character Claim (Lines 325-325)
[2025-12-25 21:01:13] [INFO] ============================================================
[2025-12-25 21:01:13] [INFO] Found 1 account(s) to process
[2025-12-25 21:01:13] [INFO] ------------------------------------------------------------
[2025-12-25 21:01:26] [INFO] [meunknown374@yahoo.com] 🎮 Found 2 characters!
[2025-12-25 21:01:26] [INFO] [meunknown374@yahoo.com] ==================================================
[2025-12-25 21:01:26] [INFO] [meunknown374@yahoo.com] Processing Character 1/2
[2025-12-25 21:01:37] [SUCCESS] [meunknown374@yahoo.com] Character 1/2: ✓ Already claimed
[2025-12-25 21:01:40] [INFO] [meunknown374@yahoo.com] ✓ Character selection opened
[2025-12-25 21:01:40] [INFO] [meunknown374@yahoo.com] ==================================================
[2025-12-25 21:01:40] [INFO] [meunknown374@yahoo.com] Processing Character 2/2
[2025-12-25 21:01:51] [SUCCESS] [meunknown374@yahoo.com] Character 2/2: ✅ Claimed 75 jewels!
[2025-12-25 21:01:51] [INFO] [meunknown374@yahoo.com] ==================================================
[2025-12-25 21:01:51] [SUCCESS] [meunknown374@yahoo.com] 💎 ACCOUNT COMPLETE: Claimed 2/2 characters
[2025-12-25 21:01:51] [INFO] ============================================================
[2025-12-25 21:01:51] [INFO] MULTI-CHARACTER BATCH SUMMARY
[2025-12-25 21:01:51] [INFO] ============================================================
[2025-12-25 21:01:51] [INFO] ✅ 2/2 chars (Line 325): meunknown374@yahoo.com
[2025-12-25 21:01:51] [INFO] ------------------------------------------------------------
[2025-12-25 21:01:51] [INFO] Accounts Processed: 1
[2025-12-25 21:01:51] [INFO] Accounts Success: 1
[2025-12-25 21:01:51] [INFO] Accounts Failed: 0
[2025-12-25 21:01:51] [INFO] Total Characters Found: 2
[2025-12-25 21:01:51] [INFO] Total Jewels Claimed: 150 💎
[2025-12-25 21:01:51] [INFO] ============================================================
```

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
- Verify `users.txt` exists in `/Users/johnalvero/godofwar/`
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

## 🚨 Why Direct API Calls Failed

The Game of War API has **anti-bot protection** that blocks:
- ❌ Simple HTTP requests (even with headers)
- ❌ n8n workflows
- ❌ Standard Python requests
- ❌ All direct API calls return 500 Internal Server Error

**Solution:** Browser automation (Playwright) is indistinguishable from a real user.

### Why Browser Automation Works

- ✅ Uses real browser (bypasses bot detection)
- ✅ Executes JavaScript (like a real user)
- ✅ Maintains proper cookies and sessions
- ✅ Has realistic timing and behavior
- ✅ Can adapt to UI changes easily
- ✅ No reverse engineering required

---

## 🔄 Migrating from Legacy Scripts

### From claim_batch.py or claim_all.py

**Old script (single character only)**:
```bash
python claim_batch.py 1 50
```

**New script (multi-character support)**:
```bash
python claim_all_characters.py 1 50
```

**Key differences**:
- ✅ `claim_all_characters.py`: Loops through ALL characters on each account
- ❌ `claim_batch.py` / `claim_all.py`: Only claims for the FIRST character

**Recommendation**: Use `claim_all_characters.py` for all future runs to maximize jewel earnings.

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

# Process all 344 accounts
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
cd /Users/johnalvero/godofwar

# 2. Activate environment
source .venv/bin/activate

# 3. Test with one account
python claim_all_characters.py 1 1

# 4. Process all accounts
python claim_all_characters.py

# 5. Set up automation (optional)
crontab -e
# Add: 0 1 * * * cd /Users/johnalvero/godofwar && source .venv/bin/activate && python claim_all_characters.py >> /Users/johnalvero/godofwar/logs/daily.log 2>&1

# 6. Monitor logs
tail -f /Users/johnalvero/godofwar/logs/daily.log
```

---

## 🆘 Support & FAQ

### Q: How long does it take to process all accounts?
**A:** ~3-4 hours for 344 accounts (30-40 seconds per account)

### Q: Can I run multiple batches simultaneously?
**A:** Not recommended - may trigger rate limiting. Use cron to run batches sequentially.

### Q: What if an account fails?
**A:** Script continues with remaining accounts. Check logs for specific error.

### Q: How do I know if jewels were actually claimed?
**A:** Look for "✅ Claimed 75 jewels!" in logs. Script verifies by checking for "Available again at" text.

### Q: Can I stop the script mid-run?
**A:** Yes, press `Ctrl+C`. It will stop gracefully after current account completes.

### Q: Do I need to keep my computer on?
**A:** Yes, for cron jobs to run. Consider using a server or always-on Mac.

---

## 📞 Contact & Issues

For issues or questions:
1. Check the **Troubleshooting** section above
2. Review console logs for error messages
3. Run in debug mode (non-headless) to observe behavior
4. Verify Game of War website is accessible
5. Check credentials in users.txt

---

## 🏆 What This Achieves

1. 🤖 **Runs daily** at scheduled times (via cron)
2. 🎮 **Opens Game of War** in headless browser
3. 🔐 **Logs in** with credentials from users.txt
4. 🎯 **Detects all characters** on each account
5. 💎 **Claims 75 jewels per character** automatically
6. 📊 **Logs detailed results** to file
7. 🔄 **Repeats for all 344 accounts** every day
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
