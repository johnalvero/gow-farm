# Game of War - Automated Jewel Claim Setup

## 🚨 Fix for n8n Error

The error you encountered was due to **missing browser headers**. I've created **2 solutions**:

### ✅ Solution 1: Python Script (Recommended - More Reliable)
### ✅ Solution 2: Updated n8n Workflow v2

---

## 🐍 Solution 1: Python Script (RECOMMENDED)

This is **more reliable** than n8n because it properly mimics a browser.

### Quick Start

1. **Test it now:**
   ```bash
   cd /Users/johnalvero/Apps/godofwar
   python3 claim_jewels.py
   ```

2. **Expected output:**
   ```
   [2025-12-25 17:50:00] [INFO] === Game of War - Daily Jewel Claim ===
   [2025-12-25 17:50:01] [INFO] Logging in to Game of War...
   [2025-12-25 17:50:02] [INFO] ✓ Login successful
   [2025-12-25 17:50:03] [INFO] Getting AppCharge token...
   [2025-12-25 17:50:04] [INFO] ✓ AppCharge token obtained
   [2025-12-25 17:50:05] [INFO] Claiming daily jewels...
   [2025-12-25 17:50:06] [SUCCESS] ✓ Successfully claimed 75 jewels!
   ```

### Automate with Cron (Mac/Linux)

Run daily at 1:00 AM:

```bash
# Open crontab editor
crontab -e

# Add this line (press 'i' to insert, then paste):
0 1 * * * /usr/bin/python3 /Users/johnalvero/Apps/godofwar/claim_jewels.py >> /Users/johnalvero/Apps/godofwar/jewel_claim.log 2>&1

# Save and exit (press ESC, type :wq, press ENTER)
```

**Verify cron job:**
```bash
crontab -l
```

**Check logs:**
```bash
tail -f /Users/johnalvero/Apps/godofwar/jewel_claim.log
```

### Automate with launchd (Mac - Preferred for macOS)

Create a launchd plist file:

```bash
# Create the plist file
cat > ~/Library/LaunchAgents/com.gameofwar.jewelclaim.plist << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.gameofwar.jewelclaim</string>

    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>/Users/johnalvero/Apps/godofwar/claim_jewels.py</string>
    </array>

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

    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF

# Load the job
launchctl load ~/Library/LaunchAgents/com.gameofwar.jewelclaim.plist

# Verify it's loaded
launchctl list | grep gameofwar
```

**To test immediately:**
```bash
launchctl start com.gameofwar.jewelclaim
```

**To stop/disable:**
```bash
launchctl unload ~/Library/LaunchAgents/com.gameofwar.jewelclaim.plist
```

### Add Notifications (Optional)

Install notification library:
```bash
pip3 install requests
# For macOS notifications:
pip3 install pync
```

Then modify the script to add notifications at the end of `main()` function.

---

## 🔄 Solution 2: Updated n8n Workflow v2

### What's Fixed in v2:
- ✅ Added proper User-Agent header
- ✅ Added Origin and Referer headers
- ✅ Better cookie extraction with Code node
- ✅ Proper Content-Type headers

### Import Instructions

1. **Delete the old workflow** (if you imported it)
2. **Import the new one:**
   - Open n8n
   - Click "+" → Import from File
   - Select `game-of-war-n8n-workflow-v2.json`
3. **Test it:**
   - Click "Schedule Trigger" node
   - Click "Execute Node" ▶️
4. **Activate it** if successful

### If n8n Still Fails

Some APIs block n8n's requests even with proper headers. If you still get errors:

**Use the Python script instead** (Solution 1) - it's more reliable.

---

## 📊 Comparison

| Feature | Python Script | n8n Workflow |
|---------|---------------|--------------|
| **Reliability** | ⭐⭐⭐⭐⭐ Excellent | ⭐⭐⭐ Good |
| **Setup** | Medium | Easy |
| **Notifications** | Requires code | Built-in |
| **Debugging** | Easy (logs) | Medium |
| **Works with anti-bot** | ✅ Yes | ⚠️ Sometimes |
| **Best for** | Technical users | Visual workflows |

---

## 🧪 Testing

### Test Python Script Now:
```bash
cd /Users/johnalvero/Apps/godofwar
python3 claim_jewels.py
```

### Test n8n Workflow:
1. Import `game-of-war-n8n-workflow-v2.json`
2. Click "Schedule Trigger" → "Execute Node"

---

## 🔍 Troubleshooting

### Python Script Errors

**"ModuleNotFoundError: No module named 'requests'"**
```bash
pip3 install requests
```

**"Permission denied"**
```bash
chmod +x /Users/johnalvero/Apps/godofwar/claim_jewels.py
```

**"Already claimed today"**
- Wait until 00:00 UTC (reset time)
- The script will work again after reset

### n8n Errors

**500 Internal Server Error (v2)**
- The API might still be blocking n8n
- **Use the Python script instead**

**401 Unauthorized**
- Check email/password in the workflow
- Session might have expired

**429 Too Many Requests**
- You're making too many requests
- Wait a few hours before trying again

---

## 📅 Schedule Reference

### UTC Time Conversions

If you're in **Philippines (GMT+8)**:
- 00:00 UTC = 8:00 AM PHT (jewels reset)
- 01:00 UTC = 9:00 AM PHT (recommended claim time)

**Cron Schedule Examples:**
```bash
# Daily at 1 AM UTC (9 AM PHT)
0 1 * * *

# Daily at 8 AM UTC (4 PM PHT)
0 8 * * *

# Every 12 hours
0 */12 * * *

# Specific time: 2:30 AM UTC
30 2 * * *
```

---

## 🎯 My Recommendation

**Use the Python script** because:
1. ✅ More reliable (better header control)
2. ✅ Works better with anti-bot systems
3. ✅ Easier to debug with logs
4. ✅ Can run anywhere (not just n8n)

Set it up with **launchd** (for Mac) or **cron** and check the logs daily to confirm it's working.

---

## 📝 Next Steps

1. **Test the Python script** → `python3 claim_jewels.py`
2. **If successful**, set up automation (launchd or cron)
3. **Check logs tomorrow** to verify it worked
4. **Optional:** Add notifications

Good luck! 🎮
