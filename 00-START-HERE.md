# 🎮 Game of War - Multi-Account Jewel Claim

## ⚡ Super Quick Start (3 Steps)

### 1️⃣ Add Accounts
```bash
nano users.txt
# Format: email:password (one per line)
```

### 2️⃣ Test It
```bash
.venv/bin/python claim_all.py
```

### 3️⃣ Automate It
```bash
crontab -e
# Add this line:
0 1 * * * cd /Users/johnalvero/Apps/godofwar && .venv/bin/python claim_all.py >> jewel_claim_all.log 2>&1
```

**Done! 💎**

---

## 📁 File Guide

| File | What It Does |
|------|--------------|
| **users.txt** | Your accounts (edit this to add more) |
| **claim_all.py** | Multi-account script ⭐ USE THIS |
| **claim_simple.py** | Single account script (for testing) |
| **FINAL_SETUP.md** | Complete setup instructions |
| **MULTI_ACCOUNT_SETUP.md** | Multi-account detailed guide |
| **QUICK_REFERENCE.md** | Command cheat sheet |

---

## 💡 users.txt Format

```
# Comments start with #
email1@example.com:password1
email2@example.com:password2
email3@example.com:password3
```

**Current:** 1 account
**To add more:** Edit `users.txt` and add one line per account

---

## 🧪 Test Commands

```bash
# Run for all accounts
.venv/bin/python claim_all.py

# View last run
tail -50 jewel_claim_all.log

# View screenshots
open screenshots/

# Count accounts
cat users.txt | grep -v "^#" | grep -v "^$" | wc -l
```

---

## 📊 Expected Results

```
Found 3 account(s) to process
------------------------------------------------------------
[1/3] Processing account: account1@...
[account1@...] ✅ Successfully claimed 75 jewels!
------------------------------------------------------------
[2/3] Processing account: account2@...
[account2@...] ✅ Successfully claimed 75 jewels!
------------------------------------------------------------
SUMMARY
✅ SUCCESS: account1@...
✅ SUCCESS: account2@...
Total: 3 | Success: 3 | Failed: 0
```

---

## 🕐 When It Runs

**Default:** Daily at 1:00 AM UTC = 9:00 AM Philippines time

**Change time?** Edit the cron expression:
- `0 1 * * *` = 1 AM
- `0 8 * * *` = 8 AM
- `0 20 * * *` = 8 PM

---

## 💰 Jewel Calculator

| Accounts | Per Day | Per Month | Per Year |
|----------|---------|-----------|----------|
| 1 | 75 | 2,250 | 27,375 |
| 3 | 225 | 6,750 | 82,125 |
| 5 | 375 | 11,250 | 136,875 |
| 10 | 750 | 22,500 | 273,750 |

---

## 🆘 Quick Troubleshooting

**Script fails?**
```bash
.venv/bin/python -m playwright install chromium --force
```

**Need to see browser?**
Edit script, change `headless=True` to `headless=False`

**Permission denied?**
```bash
chmod 600 users.txt
chmod +x claim_all.py
```

**Check if working?**
```bash
tail -50 jewel_claim_all.log
open screenshots/
```

---

## 📚 Full Documentation

- **Quick Start:** This file (00-START-HERE.md)
- **Full Setup:** FINAL_SETUP.md
- **Multi-Account:** MULTI_ACCOUNT_SETUP.md
- **Commands:** QUICK_REFERENCE.md
- **Fixes:** QUICK_FIX.md

---

## ✅ Status

- ✅ Scripts working
- ✅ Cookie consent fixed
- ✅ Tested successfully
- ⚠️ Automation setup (you need to do this)

---

## 🎯 What Happens

```
Daily at 1 AM UTC
    ↓
Read users.txt
    ↓
For each account:
  - Login
  - Claim 75 jewels
  - Save screenshot
    ↓
Log results
```

**Fast. Simple. Automated. Free.** 💎

---

**Ready? Run:** `.venv/bin/python claim_all.py` 🚀
