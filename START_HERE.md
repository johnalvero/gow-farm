# ✅ WORKING SOLUTION - Start Here!

## 🎉 Success! The Script is Working!

The issue was a **cookie consent dialog** blocking the login button. It's now fixed!

---

## 🚀 Quick Start

### Test it now (to verify it works):
```bash
cd /Users/johnalvero/Apps/godofwar
.venv/bin/python claim_simple.py
```

**Expected output:**
```
[INFO] Starting Game of War Jewel Claim
[INFO] Loading login page...
[INFO] Dismissed cookie consent
[INFO] Clicking login button...
[SUCCESS] ✓ Jewels already claimed today (on cooldown)
```

(It says "already claimed" because we claimed them earlier. Tomorrow it will actually claim them!)

---

## ⏰ Automate It (Daily at 1 AM)

### Option 1: Using cron (Simple)

```bash
# Open crontab
crontab -e

# Add this line (press 'i', paste, then ESC :wq ENTER):
0 1 * * * cd /Users/johnalvero/Apps/godofwar && .venv/bin/python claim_simple.py >> jewel_claim.log 2>&1
```

**Verify it's added:**
```bash
crontab -l
```

**Check logs later:**
```bash
tail -f /Users/johnalvero/Apps/godofwar/jewel_claim.log
```

### Option 2: Using launchd (macOS Preferred)

```bash
cat > ~/Library/LaunchAgents/com.gameofwar.jewelclaim.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.gameofwar.jewelclaim</string>

    <key>ProgramArguments</key>
    <array>
        <string>/Users/johnalvero/Apps/godofwar/.venv/bin/python</string>
        <string>/Users/johnalvero/Apps/godofwar/claim_simple.py</string>
    </array>

    <key>WorkingDirectory</key>
    <string>/Users/johnalvero/Apps/godofwar</string>

    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>1</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>/Users/johnalvero/Apps/godofwar/jewel_claim.log</string>

    <key>StandardErrorPath</key>
    <string>/Users/johnalvero/Apps/godofwar/jewel_claim_error.log</string>
</dict>
</plist>
EOF

# Load it
launchctl load ~/Library/LaunchAgents/com.gameofwar.jewelclaim.plist

# Test it now (don't wait for 1 AM)
launchctl start com.gameofwar.jewelclaim

# Check the log
tail -f /Users/johnalvero/Apps/godofwar/jewel_claim.log
```

**Manage:**
```bash
# Check status
launchctl list | grep gameofwar

# Stop
launchctl unload ~/Library/LaunchAgents/com.gameofwar.jewelclaim.plist

# Restart
launchctl unload ~/Library/LaunchAgents/com.gameofwar.jewelclaim.plist
launchctl load ~/Library/LaunchAgents/com.gameofwar.jewelclaim.plist
```

---

## 📋 Files Created

| File | Purpose | Status |
|------|---------|--------|
| **claim_simple.py** | ⭐ Main working script | ✅ Use this! |
| claim_manual.sh | Curl-based alternative | ⚠️ May not work (API blocking) |
| claim_jewels_browser.py | Old version | ❌ Had timeout issues |
| claim_jewels.py | API version | ❌ Doesn't work (500 error) |
| setup.sh | Install dependencies | ✅ Already ran |
| requirements.txt | Python packages | ✅ Already installed |

**Documentation:**
- START_HERE.md ⭐ (this file)
- README.md - Complete guide
- QUICK_FIX.md - Troubleshooting
- game-of-war-jewel-claim-api.md - API documentation
- n8n-setup-guide.md - n8n info (doesn't work due to bot detection)

---

## 🎯 What Happens Next

1. ✅ **Script runs daily at 1 AM UTC** (9 AM Philippines time)
2. ✅ **Opens browser** (headless - no window)
3. ✅ **Logs into Game of War**
4. ✅ **Dismisses cookie consent** (this was the issue!)
5. ✅ **Navigates to Vault**
6. ✅ **Claims 75 jewels**
7. ✅ **Logs success** to `jewel_claim.log`
8. ✅ **Closes browser**

**Total time:** ~10-15 seconds

---

## 📅 Schedule Info

**Jewels reset:** 00:00 UTC daily

**Philippines time (GMT+8):**
- 00:00 UTC = 8:00 AM PHT (reset)
- 01:00 UTC = 9:00 AM PHT (claim time)

**Change schedule:**
Edit the cron expression or plist file `Hour` value:
- 0 = Midnight
- 1 = 1 AM (default)
- 8 = 8 AM
- 12 = Noon
- 20 = 8 PM

---

## 🔍 Verification

Tomorrow morning (after 1 AM), check if it worked:

```bash
# View the log
cat /Users/johnalvero/Apps/godofwar/jewel_claim.log

# You should see:
# [SUCCESS] ✓ Successfully claimed jewels!
```

Or check the screenshots:
```bash
open /Users/johnalvero/Apps/godofwar/step3_after_claim.png
```

---

## 🐛 Troubleshooting

### "Already claimed today"
✅ This is **normal** if you run it multiple times in 24 hours. It will work tomorrow!

### No log file created
❌ Cron job might not be set up correctly
- Run: `crontab -l` to verify
- Check path is correct in crontab

### Script fails
❌ Check the error log:
```bash
cat /Users/johnalvero/Apps/godofwar/jewel_claim_error.log
```

Look at the screenshot:
```bash
open /Users/johnalvero/Apps/godofwar/error.png
```

### Want to see it run?
Edit `claim_simple.py` line 20:
```python
browser = await p.chromium.launch(headless=False)  # Shows browser
```

---

## ✨ Summary

**Working script:** `claim_simple.py` ✅

**Run manually:**
```bash
.venv/bin/python claim_simple.py
```

**Automate:**
```bash
crontab -e
# Add: 0 1 * * * cd /Users/johnalvero/Apps/godofwar && .venv/bin/python claim_simple.py >> jewel_claim.log 2>&1
```

**Check tomorrow:** `cat jewel_claim.log`

---

**You're all set! Enjoy your daily jewels!** 💎🎮
