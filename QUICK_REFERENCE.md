# 📚 Quick Reference Guide

## 🎯 Which Script to Use?

| Scenario | Use This | Command |
|----------|----------|---------|
| **Multiple accounts** | `claim_all.py` ⭐ | `.venv/bin/python claim_all.py` |
| Single account | `claim_simple.py` | `.venv/bin/python claim_simple.py` |
| Testing/debugging | `claim_simple.py` | `.venv/bin/python claim_simple.py` |

---

## 📝 Users.txt Format

```
# Comments start with #
email1@example.com:password1
email2@example.com:password2
email3@example.com:password3
```

**Rules:**
- One account per line
- Format: `email:password`
- No spaces around `:`
- `#` lines are ignored

---

## ⚡ Quick Commands

### Test Multi-Account Script
```bash
cd /Users/johnalvero/Apps/godofwar
.venv/bin/python claim_all.py
```

### Test Single Account
```bash
.venv/bin/python claim_simple.py
```

### Add New Account
```bash
echo "newemail@example.com:newpassword" >> users.txt
```

### View Accounts
```bash
cat users.txt | grep -v "^#" | grep -v "^$"
```

### View Last Run Log
```bash
tail -50 jewel_claim_all.log
```

### View Screenshots
```bash
open screenshots/
```

### Clean Old Logs
```bash
> jewel_claim_all.log  # Clear log file
```

---

## 🔧 Automation Setup

### Cron (Simple)
```bash
crontab -e
# Add:
0 1 * * * cd /Users/johnalvero/Apps/godofwar && .venv/bin/python claim_all.py >> jewel_claim_all.log 2>&1
```

### Launchd (macOS)
```bash
# Load
launchctl load ~/Library/LaunchAgents/com.gameofwar.jewelclaim.plist

# Unload
launchctl unload ~/Library/LaunchAgents/com.gameofwar.jewelclaim.plist

# Test now
launchctl start com.gameofwar.jewelclaim

# Check status
launchctl list | grep gameofwar
```

---

## 📊 Log Commands

### View Summary
```bash
grep "SUMMARY" -A 10 jewel_claim_all.log | tail -15
```

### Count Successes
```bash
grep "✅ SUCCESS" jewel_claim_all.log | wc -l
```

### Count Failures
```bash
grep "❌ FAILED" jewel_claim_all.log | wc -l
```

### See Failed Accounts
```bash
grep "❌ FAILED" jewel_claim_all.log
```

### Watch Live (during run)
```bash
tail -f jewel_claim_all.log
```

---

## 🔐 Security Commands

### Restrict users.txt Permissions
```bash
chmod 600 users.txt
```

### Backup users.txt
```bash
cp users.txt users.txt.backup
```

### Encrypt users.txt
```bash
gpg -c users.txt
rm users.txt
# To decrypt: gpg users.txt.gpg
```

---

## 📁 File Locations

```
/Users/johnalvero/Apps/godofwar/
├── users.txt                    ← Your accounts
├── claim_all.py                ← Multi-account script ⭐
├── claim_simple.py             ← Single account script
├── jewel_claim_all.log         ← Log file
├── screenshots/                ← Screenshots folder
├── MULTI_ACCOUNT_SETUP.md      ← Full multi-account guide
├── START_HERE.md               ← Quick start guide
└── QUICK_REFERENCE.md          ← This file
```

---

## 🕐 Schedule Times

| Time (UTC) | Philippines (GMT+8) | Description |
|------------|---------------------|-------------|
| 00:00 | 8:00 AM | Jewels reset |
| 01:00 | 9:00 AM | Default claim time ⭐ |
| 08:00 | 4:00 PM | Alternative time |
| 12:00 | 8:00 PM | Alternative time |

---

## ❓ Troubleshooting Quick Fixes

### Script won't run
```bash
chmod +x claim_all.py
.venv/bin/python claim_all.py
```

### No accounts found
```bash
cat users.txt
# Make sure format is: email:password
```

### Permission denied
```bash
chmod 600 users.txt
chmod +x claim_all.py
```

### Browser timeout
```bash
# In claim_all.py or claim_simple.py, change:
headless=False  # Shows browser window
```

### Check if website is up
```bash
curl -I https://www.gameofwar-fireage.com
```

### Reinstall Playwright browser
```bash
.venv/bin/python -m playwright install chromium --force
```

---

## 📸 Screenshot Naming

Format: `{email_sanitized}_{action}.png`

Examples:
- `meunknown374_at_yahoo_com_catalog.png`
- `meunknown374_at_yahoo_com_after_claim.png`
- `player2_at_gmail_com_catalog.png`

View all:
```bash
ls -lh screenshots/
open screenshots/
```

---

## 🎮 Account Management

### Add Account
```bash
echo "email@example.com:password" >> users.txt
```

### Disable Account (keep in file)
```bash
# Edit users.txt and add # before the line:
# email@example.com:password
```

### Remove Account
```bash
# Edit users.txt and delete the line
nano users.txt
```

### Count Active Accounts
```bash
grep -v "^#" users.txt | grep -v "^$" | wc -l
```

---

## 🚀 Testing Checklist

Before setting up automation:

- [ ] Added accounts to `users.txt`
- [ ] Tested: `.venv/bin/python claim_all.py`
- [ ] All accounts succeeded
- [ ] Checked screenshots in `screenshots/`
- [ ] Reviewed `jewel_claim_all.log`
- [ ] Set up cron/launchd
- [ ] Tested automation: `launchctl start com.gameofwar.jewelclaim`
- [ ] Will check logs tomorrow

---

## 📚 Documentation Files

| File | Purpose |
|------|---------|
| **MULTI_ACCOUNT_SETUP.md** | Complete multi-account guide |
| **START_HERE.md** | Quick start for single account |
| **QUICK_REFERENCE.md** | This file - quick commands |
| **README.md** | Full documentation |
| **QUICK_FIX.md** | Troubleshooting guide |

---

## 💡 Pro Tips

1. **Test first:** Always run manually before automating
2. **Check logs:** Review logs daily for first week
3. **Backup users.txt:** `cp users.txt users.txt.backup`
4. **Use screenshots:** They show exactly what happened
5. **Stagger accounts:** Add 10s delay between them (already built-in!)

---

## 🆘 Help

**Script not working?**
1. Check logs: `tail -50 jewel_claim_all.log`
2. View screenshots: `open screenshots/`
3. Test manually: `.venv/bin/python claim_all.py`
4. Check permissions: `ls -l users.txt`

**Need more help?**
- Read: `MULTI_ACCOUNT_SETUP.md`
- Read: `START_HERE.md`
- Check: `QUICK_FIX.md`

---

**Quick Start:** Add accounts to `users.txt` → Run `claim_all.py` → Check logs & screenshots! 🎮
