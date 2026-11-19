# 🎮 Game of War - Automated Multi-Account Jewel Claiming System

Automated browser-based system for claiming free daily jewels (75 per character) from Game of War accounts. Supports **multi-character accounts** and **batch processing** for up to 344 accounts.

---

## ✨ Key Features

- ✅ **Multi-Character Support**: Automatically detects and claims jewels for ALL characters on each account
- ✅ **Batch Processing**: Process all 344 accounts or specific ranges
- ✅ **Anti-Bot Detection Bypass**: Uses real browser automation (Playwright) to avoid detection
- ✅ **Detailed Logging**: Timestamped logs with success/failure tracking
- ✅ **Screenshot Capture**: Saves screenshots for verification (optional)
- ✅ **Bounded Parallelism**: 3 accounts at a time by default, each in its own isolated session on one shared browser
- ✅ **Rate Limiting**: At most one account start every 10 seconds to avoid triggering rate limits
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
cd /Users/johnalvero/Apps/godofwar

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
   cd /Users/johnalvero/Apps/godofwar
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
├── claim_common.py              # Shared login, claim and run code
├── dashboard/                   # Local dashboard (see Dashboard below)
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
your-email@example.com:your-password
user123@example.com:password123
your-email@example.com:your-password
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

**Expected runtime**: roughly 1-1.5 hours for all 344 accounts at the defaults. This is an estimate, as logged-in timing has not been measured. At most one account starts every 10 seconds, so a full run can't finish in under about 57 minutes unless `--stagger` is lowered.

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
mkdir -p /Users/johnalvero/Apps/godofwar/logs
```

Edit your crontab:
```bash
crontab -e
```

Add the following schedules (7 batches from 1 AM - 7 AM UTC):

```bash
# Game of War Jewel Claiming - Batch 1 (Lines 1-50)
0 1 * * * cd /Users/johnalvero/Apps/godofwar && source .venv/bin/activate && python claim_all_characters.py 1 50 >> /Users/johnalvero/Apps/godofwar/logs/batch1.log 2>&1

# Game of War Jewel Claiming - Batch 2 (Lines 51-100)
0 2 * * * cd /Users/johnalvero/Apps/godofwar && source .venv/bin/activate && python claim_all_characters.py 51 100 >> /Users/johnalvero/Apps/godofwar/logs/batch2.log 2>&1

# Game of War Jewel Claiming - Batch 3 (Lines 101-150)
0 3 * * * cd /Users/johnalvero/Apps/godofwar && source .venv/bin/activate && python claim_all_characters.py 101 150 >> /Users/johnalvero/Apps/godofwar/logs/batch3.log 2>&1

# Game of War Jewel Claiming - Batch 4 (Lines 151-200)
0 4 * * * cd /Users/johnalvero/Apps/godofwar && source .venv/bin/activate && python claim_all_characters.py 151 200 >> /Users/johnalvero/Apps/godofwar/logs/batch4.log 2>&1

# Game of War Jewel Claiming - Batch 5 (Lines 201-250)
0 5 * * * cd /Users/johnalvero/Apps/godofwar && source .venv/bin/activate && python claim_all_characters.py 201 250 >> /Users/johnalvero/Apps/godofwar/logs/batch5.log 2>&1

# Game of War Jewel Claiming - Batch 6 (Lines 251-300)
0 6 * * * cd /Users/johnalvero/Apps/godofwar && source .venv/bin/activate && python claim_all_characters.py 251 300 >> /Users/johnalvero/Apps/godofwar/logs/batch6.log 2>&1

# Game of War Jewel Claiming - Batch 7 (Lines 301-344)
0 7 * * * cd /Users/johnalvero/Apps/godofwar && source .venv/bin/activate && python claim_all_characters.py 301 344 >> /Users/johnalvero/Apps/godofwar/logs/batch7.log 2>&1
```

### Verify Cron Jobs

```bash
# List all cron jobs
crontab -l

# Check logs
tail -f /Users/johnalvero/Apps/godofwar/logs/batch1.log
```

---

## 🔍 How It Works

### Multi-Character Flow

1. **Login**
   - Navigate to login page
   - Fill credentials
   - Dismiss cookie consent (OneTrust)
   - Click login button
   - With no character dialog, wait for sign-in to finish

2. **Character Detection**
   - Detect all available characters in character selection dialog
   - Log total characters found (e.g., "🎮 Found 2 characters!")

3. **Character Loop** (for each character):
   - Select the character, and wait for the sign-in that follows it to finish
   - Navigate to vault (`/catalog/main`)
   - Check for "Claim Now!" button
   - Wait for the page's session to load, because a click before that logs the player out
   - Click claim button → 75 jewels
   - Verify success (the claim request is accepted by the server)
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
- **Verification**: Reads the `/api/appcharge/collect-offer` response after the click, and only an accepted response counts as a claim
- **Error recovery**: Continues processing remaining accounts even if one fails
- **Parallelism**: Up to 3 accounts in flight (`--concurrency`), one browser, one isolated context per account
- **Rate limiting**: At most one account start every 10 seconds (`--stagger`)

---

## 📈 Output Format

### Console Output Example

```
[2025-12-25 21:01:13] [INFO] ============================================================
[2025-12-25 21:01:13] [INFO] Game of War - Multi-Character Claim (Lines 325-325)
[2025-12-25 21:01:13] [INFO] ============================================================
[2025-12-25 21:01:13] [INFO] Found 1 account(s) to process
[2025-12-25 21:01:13] [INFO] ------------------------------------------------------------
[2025-12-25 21:01:26] [INFO] [user@example.com] 🎮 Found 2 characters!
[2025-12-25 21:01:26] [INFO] [user@example.com] ==================================================
[2025-12-25 21:01:26] [INFO] [user@example.com] Processing Character 1/2
[2025-12-25 21:01:37] [SUCCESS] [user@example.com] Character 1/2: ✓ Already claimed
[2025-12-25 21:01:40] [INFO] [user@example.com] ✓ Character selection opened
[2025-12-25 21:01:40] [INFO] [user@example.com] ==================================================
[2025-12-25 21:01:40] [INFO] [user@example.com] Processing Character 2/2
[2025-12-25 21:01:51] [SUCCESS] [user@example.com] Character 2/2: ✅ Claimed 75 jewels!
[2025-12-25 21:01:51] [INFO] [user@example.com] ==================================================
[2025-12-25 21:01:51] [SUCCESS] [user@example.com] 💎 ACCOUNT COMPLETE: Claimed 2/2 characters (1 already claimed)
[2025-12-25 21:01:51] [INFO] ============================================================
[2025-12-25 21:01:51] [INFO] MULTI-CHARACTER BATCH SUMMARY
[2025-12-25 21:01:51] [INFO] ============================================================
[2025-12-25 21:01:51] [INFO] ✅ 2/2 chars (Line 325): user@example.com
[2025-12-25 21:01:51] [INFO] ------------------------------------------------------------
[2025-12-25 21:01:51] [INFO] Accounts Processed: 1
[2025-12-25 21:01:51] [INFO] Accounts Success: 1
[2025-12-25 21:01:51] [INFO] Accounts Failed: 0
[2025-12-25 21:01:51] [INFO] Total Characters Found: 2
[2025-12-25 21:01:51] [INFO] Characters Already Claimed: 1
[2025-12-25 21:01:51] [INFO] Total Jewels Claimed: 75 💎
[2025-12-25 21:01:51] [INFO] ============================================================
```

### Status Indicators

- ✅ **Claimed successfully**: Jewels were claimed this run
- ✓ **Already claimed**: Jewels were already claimed today (on cooldown)
- ⚠ **Warning**: Non-critical issue (e.g., a slow character selection or a screenshot that could not be saved)
- ❌ **Failed**: Account processing failed completely
- 🎮 **Found N characters**: Detected multi-character account
- 💎 **Total Jewels Claimed**: New claims from this run only. Characters already on cooldown add nothing and are counted under `Characters Already Claimed`

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
- Verify `users.txt` exists in `/Users/johnalvero/Apps/godofwar/`
- Check file permissions

**3. "Timeout" errors**
- Increase the `page.goto` timeouts in `claim_common.py` (change `timeout=60000` to `timeout=120000`)
- Check internet connection
- Game of War website might be slow or down

**4. Cookie consent blocking**
- Already handled automatically (`dismiss_cookie_banner` in `claim_common.py`)
- If persists, verify OneTrust selectors haven't changed

**5. Character switching fails**
- Verify "Change Account" button exists (might vary by account type)
- Check the `Change Account` selector in `claim_for_account` in `claim_all_characters.py`
- Try debug mode to see what's happening

**6. False "Already claimed" status**
- An account is only reported as already claimed when a cooldown message is visible and no Claim button is
- See `vault_state` in `claim_common.py`

**7. Only claiming first character**
- Make sure you're using `claim_all_characters.py` (not legacy scripts)
- Check the log for `Opening character selection...` and `✓ Character selection opened` after each character

**8. "Account deadline of 900s passed"**
- An account that runs for 15 minutes is stopped and reported as `❌ FAILED`, so a frozen page can't hold up the rest of the run
- Characters it finished before that still count in the totals, and the next run picks up the rest
- A claim in flight when it stopped is not counted, even if it landed. The next run shows it as already claimed

### Debug Mode

To run with visible browser for debugging:

1. Edit `BrowserHolder.get()` in `claim_common.py` (this applies to all three scripts)
2. Change `headless=True` to `headless=False`
3. Run script with `--concurrency 1` - a single browser window will be visible
4. Watch the automation in real-time

To check whether resource blocking is causing a problem, add `--no-block`. It is slow and for diagnosis only, and a timeout under it proves nothing either way.

### Logs Location

- **Console output**: Shown in terminal
- **Cron logs**: `/Users/johnalvero/Apps/godofwar/logs/batch*.log`
- **Screenshots**: `/Users/johnalvero/Apps/godofwar/screenshots/`
- **Failed claims**: `screenshots/<email>_claim_failed.png`, always saved by all three scripts, overwritten per account
- **Vault views**: `claim_all.py` also saves `<email>_catalog.png` and `<email>_after_claim.png`

---

## 🔒 Security Notes

- ⚠️ **Keep users.txt secure**: Contains plaintext passwords
- ⚠️ **Do not commit users.txt to Git**: Should be in `.gitignore`
- ⚠️ **Backup users.txt regularly**: Lost credentials cannot be recovered
- ⚠️ **Restrict file permissions**:
  ```bash
  chmod 600 /Users/johnalvero/Apps/godofwar/users.txt
  ```

### Optional: Use Environment Variables

To keep the accounts file somewhere else, set `GOW_USERS_FILE`. All three scripts read it, so nothing needs editing:

```bash
# Set environment variables
export GOW_USERS_FILE="/Users/johnalvero/Apps/godofwar/users.txt"
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
- ✅ `claim_batch.py` / `claim_all.py`: Claim normally on single-character accounts
- ❌ `claim_batch.py` / `claim_all.py`: Never choose a character, so a multi-character account reports `❌ FAILED` and claims nothing

**Recommendation**: Use `claim_all_characters.py` for all future runs to maximize jewel earnings.

---

## 📊 Dashboard

A local page showing claim runs, with controls to start, stop and retry them. It also shows runs started from the terminal or from cron, each account by its email, and the run's seconds per claim.

```bash
.venv/bin/python dashboard/server.py            # http://127.0.0.1:8765/
.venv/bin/python dashboard/server.py --port 9000
```

A claim run started from a terminal opens the dashboard in your browser by itself, and prints one `Dashboard` line. If the server isn't running, the run starts it in the background, and it keeps running after the run ends. The server's own output goes to `dashboard/data/dashboard-server.log`. A server started this way has no terminal, so `Ctrl+C` can't stop it: find its pid with `lsof -i :8765` and `kill <pid>`. If the always-on service is installed (see below), use `bash dashboard/service.sh uninstall` instead. Add `--no-dashboard` to skip it for one run, or set `GOW_DASHBOARD_PORT` to use another port, or set it empty to turn this off. Runs started by the dashboard, from cron or without a terminal, and runs with recording turned off never open it.

- **Local only**: the server listens on `127.0.0.1`, and the page loads nothing from the network.
- **Start run** processes every account in `users.txt` at the chosen number of browsers, with no confirmation prompt. It refuses while any run is live, including one started from a terminal.
- **Stop** acts like `Ctrl+C`: accounts in flight are abandoned and no summary is printed, but the page keeps the partial results. It only works on a run this server started. For a terminal run, press `Ctrl+C` in that terminal.
- **Retry** reruns the latest run's failed accounts and accounts with characters left unclaimed, using `--lines`. Accounts a crashed run never finished are shown as unfinished and are not retried. Retry also skips accounts a stopped run never reached, so use **Start run** to cover those.
- **Seconds per claim** is the run's time so far divided by its new claims, live and in Recent runs. It includes stagger, retries and time spent on failed accounts, so it shows the run's real pace, not how long one claim takes. Already-claimed accounts don't count as claims.
- **Days use UTC**, to match the 00:00 UTC jewel reset.
- Stopping the server leaves any run going.
- An open page reloads itself after the server restarts. It never repeats an action.

### Always-on dashboard

This sets up a Mac so the dashboard is always there: you just open the browser and start runs from the page.

- It starts by itself when you log in to the Mac, and starts again by itself if it ever stops.
- It keeps the Mac from going to sleep on its own, so runs (including cron runs) aren't cut short. The screen still turns off. To let the Mac sleep as usual, install with `bash dashboard/service.sh install --allow-sleep` instead.
- The page works only in a browser **on that same Mac**, at `http://127.0.0.1:8765/`. Other computers and phones can't open it.

**Moving the project to the other Mac**

1. Set up the project on that Mac first (see Installation & Setup), so it has its own `.venv` folder with Playwright in it.
2. Copy the project folder over, but **don't replace that Mac's `.venv` folder**: the one from this Mac won't work there.
3. Put the folder in your home folder, for example `~/Apps/godofwar`. **Not** on the Desktop or in Documents or Downloads: macOS stops the service from reading files there, and install refuses those places.
4. Copy `users.txt` only with a USB stick or AirDrop. Never send it by email or put it in a cloud folder: it holds every account's password.

**The commands**

Open Terminal, go to the project folder, then run the one you need. Don't put `sudo` in front: they refuse to run as root.

```bash
cd ~/Apps/godofwar

bash dashboard/service.sh install     # set it up and start it
bash dashboard/service.sh status      # check that it's working
bash dashboard/service.sh uninstall   # stop it and remove it
```

- `install` checks the project first, and changes nothing if something is missing. When it works, it prints `The dashboard is running: http://127.0.0.1:8765/` and `It will now start by itself when you log in.` If a dashboard you started yourself is already open, it shows it and asks `Stop it so the service can take over? [y/N]`. It won't do anything while a claim run is going: run it again afterwards.
- `status` prints three lines. When everything is working, they are:
  ```
  Installed: yes (/Users/<you>/Library/LaunchAgents/com.godofwar.dashboard.plist)
  Loaded: yes
  Answering: yes, http://127.0.0.1:8765/
  ```
  `Answering: no` for more than a minute means it isn't running: look at its log.
- `uninstall` prints `Removed. The dashboard will no longer start by itself.` Your run history and logs are kept. It won't do anything while a claim run is going: run it again afterwards.

To stop the dashboard, use `uninstall`. With the service installed, `kill` doesn't work: the dashboard starts again by itself within about 30 seconds.

**Energy Saver settings on that Mac**

Open System Preferences > Energy Saver (on newer Macs, System Settings > Battery > Power Adapter, or Energy):

- Keep the Mac plugged in. Letting the display sleep is fine.
- Turn on **Prevent computer from sleeping automatically when the display is off**.
- Turn on **Start up automatically after a power failure**.

A laptop still sleeps when its lid is closed. The service starts only once someone logs in, so after a restart nothing runs until you log in. To have it start by itself after a power cut, turn on automatic login (System Preferences > Users & Groups > Login Options). Automatic login isn't available when FileVault is on.

**Logs**

The service's log is `dashboard/data/dashboard-service.log` in the project folder. Past 1 MB, the old part moves to `dashboard-service.log.1`. A dashboard run's own output still goes to `dashboard/data/last-run.log`.

### Connection status

The strip at the top of the page shows whether the internet and the game site can be reached. A connection problem never pauses or stops a run. If the status isn't good, Start and Retry ask you to confirm with `Start anyway` or `Retry anyway`, and during a run a banner warns that accounts may be failing.

While the page is open or a run is live, the server contacts:

- `https://www.gstatic.com/generate_204` every 30 seconds, to check the internet. Only a 204 answer counts, so a captive portal reads as no internet. This tells Google the machine is online. Set `GOW_INTERNET_CHECK_URL` to use another target.
- The game site's login page every 2 minutes, with a `HEAD` request. It carries no cookies and no account data. Any answer below 500 counts as reachable, so `game site OK` means the site answered, not that claims will work.

The checks ignore system proxies, and the result is not saved. With neither the page nor a run active, no checks are made.

### What is stored

Scripts append to `dashboard/data/events.jsonl`, which is git-ignored and readable by you only. It holds full account emails, line numbers, stages, fixed failure reasons and HTTP statuses. It never holds a password or any text from the site.

Only a value shaped like an email is recorded in full. Anything else, such as a username or a `users.txt` line with its two parts swapped, stays masked as `j***`, so a misplaced password can't reach the file. An email-shaped password on a swapped line can't be told apart, and would be recorded. Runs recorded before this keep their masked labels, so search finds those by line number.

Websites can't read the page's data, but any program running on this Mac can, including those emails, from `http://127.0.0.1:8765/api/state`. Past 20 MB it rotates to `events.jsonl.1`.

A dashboard run's full output goes to `dashboard/data/last-run.log`, overwritten per run and never served. Like any cron log, it holds full emails.

Recording can't fail or slow a claim. Set `GOW_EVENTS_FILE` to use another file, or set it empty to turn recording off.

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
python claim_all_characters.py --lines 2,7,9   # Only these lines (claim_all_characters.py only)

# Tune parallelism (works on claim_all_characters.py, claim_batch.py and claim_all.py)
python claim_all_characters.py 1 50 --concurrency 2       # 2 accounts at a time
python claim_all_characters.py --concurrency 1            # One account at a time
python claim_all_characters.py 1 50 --stagger 15          # 15 seconds between account starts
python claim_all_characters.py 1 1 --no-block             # Diagnosis only: load everything, slowly
python claim_all_characters.py --concurrency 6 --force-concurrency   # Above the limit of 5
python claim_all_characters.py 1 50 --no-dashboard        # Don't open or start the dashboard

# Check which account is on which line
grep -n "email@example.com" users.txt
```

### Parallelism Options

| Option | Default | Meaning |
|--------|---------|---------|
| `--concurrency N` | `3` | Most accounts in flight at once (1 to 5) |
| `--stagger SECONDS` | `10` | Minimum gap between account starts across the whole run (0 or more) |
| `--force-concurrency` | off | Allow `--concurrency` above 5 |
| `--no-block` | off | Turn off all resource blocking, for diagnosis only |
| `--no-dashboard` | off | Don't open or start the dashboard |

- Options can go before or after the line numbers. Invalid values exit with code 1 before anything runs.
- Lowering `--stagger` below about 10 seconds raises the login rate from one IP and may trigger rate limiting.
- Stagger applies whenever an account takes under about 30 seconds (concurrency × stagger), so raising `--concurrency` above 3 does little unless `--stagger` is lowered too.
- Images are replaced with a blank placeholder to save time, so `claim_all.py` screenshots show layout and text but no images.

---

## 🎯 Quick Start Summary

```bash
# 1. Navigate to project
cd /Users/johnalvero/Apps/godofwar

# 2. Activate environment
source .venv/bin/activate

# 3. Test with one account
python claim_all_characters.py 1 1

# 4. Process all accounts
python claim_all_characters.py

# 5. Set up automation (optional)
crontab -e
# Add: 0 1 * * * cd /Users/johnalvero/Apps/godofwar && source .venv/bin/activate && python claim_all_characters.py >> /Users/johnalvero/Apps/godofwar/logs/daily.log 2>&1

# 6. Monitor logs
tail -f /Users/johnalvero/Apps/godofwar/logs/daily.log
```

---

## 🆘 Support & FAQ

### Q: How long does it take to process all accounts?
**A:** Roughly 1-1.5 hours for 344 accounts at the defaults (an estimate). Accounts run 3 at a time, with at most one start every 10 seconds.

### Q: Can I run multiple batches simultaneously?
**A:** Not recommended - each run paces itself, so overlapping runs add up and may trigger rate limiting. Use cron to run batches sequentially, and `--concurrency` for parallelism within a run.

### Q: What if an account fails?
**A:** Script continues with remaining accounts. Check logs for specific error.

### Q: How do I know if jewels were actually claimed?
**A:** Look for "✅ Claimed 75 jewels!" in logs. The script reads the `/api/appcharge/collect-offer` response after clicking, and counts a claim only if the server accepted it. If not, it logs `Claim failed` with the reason, any console errors from the page, and saves `screenshots/<email>_claim_failed.png`.

### Q: Can I stop the script mid-run?
**A:** Yes, press `Ctrl+C`. It stops straight away: in-flight accounts are abandoned and no summary is printed, so check the log and re-run the affected lines.

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
