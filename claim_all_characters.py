#!/usr/bin/env python3
"""
Game of War - Multi-Character Batch Jewel Claim
Processes a range of accounts and claims for ALL characters on each account
"""

import asyncio
import sys
import os

from claim_common import (
    ALREADY_CLAIMED,
    CARD_COUNT_STABLE_MS,
    CARD_COUNT_TIMEOUT_MS,
    CLAIMED,
    FAILED,
    JEWELS_PER_CLAIM,
    PlaywrightError,
    PlaywrightTimeoutError,
    account_page,
    exit_if_stuck,
    first_line,
    is_valid_email_field,
    log,
    log_traceback,
    login,
    parse_run_options,
    run_accounts,
    say,
    select_character,
    submit_claim,
    vault_state,
    wait_for_stable_count,
    wait_interactive,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.environ.get("GOW_USERS_FILE", os.path.join(BASE_DIR, "users.txt"))
SCREENSHOT_DIR = os.path.join(BASE_DIR, "screenshots")

USAGE = """Usage: python claim_all_characters.py [<start_line> <end_line> | --lines N,N] [--concurrency N] [--stagger SECONDS] [--no-block] [--force-concurrency] [--no-dashboard]
Examples:
  python claim_all_characters.py           # Process all accounts
  python claim_all_characters.py 1 50      # Process accounts 1-50
  python claim_all_characters.py --lines 2,7,9   # Process only those lines
  python claim_all_characters.py 1 50 --concurrency 2 --stagger 15"""

def read_accounts(start_line, end_line):
    """Read accounts from users.txt within the specified range"""
    accounts = []

    if not os.path.exists(USERS_FILE):
        log(f"Users file not found: {USERS_FILE}", "ERROR")
        return accounts

    with open(USERS_FILE, 'r') as f:
        lines = f.readlines()

    start_idx = start_line - 1
    end_idx = min(end_line, len(lines))

    for line_num in range(start_idx, end_idx):
        line = lines[line_num].strip()

        if not line or line.startswith('#'):
            continue

        if ':' not in line:
            log(f"Invalid format on line {line_num + 1}", "WARNING")
            continue

        email, password = line.split(':', 1)
        email = email.strip()
        if not is_valid_email_field(email):
            log(f"Invalid format on line {line_num + 1}", "WARNING")
            continue

        password = password.strip()

        if email and password:
            accounts.append({
                'email': email,
                'password': password,
                'line_num': line_num + 1
            })

    return accounts

async def claim_for_character(page, email, password, failure_screenshot, character_num, total_characters):
    """Claim jewels for a specific character (already selected)"""
    try:
        # Navigate to vault
        log(f"[{email}] Character {character_num}/{total_characters}: Navigating to vault...")
        state = await vault_state(page, email)

        # Try to claim
        if state == "claim":
            log(f"[{email}] Character {character_num}/{total_characters}: Found Claim button, clicking...")

            # Only the server's response counts as a claim
            if await submit_claim(page, email, password, failure_screenshot):
                log(f"[{email}] Character {character_num}/{total_characters}: ✅ Claimed 75 jewels!", "SUCCESS")
                return CLAIMED
            else:
                log(f"[{email}] Character {character_num}/{total_characters}: ✗ Claim failed", "ERROR")
                return FAILED
        else:
            # Check if already on cooldown
            if state == "cooldown":
                log(f"[{email}] Character {character_num}/{total_characters}: ✓ Already claimed", "SUCCESS")
                return ALREADY_CLAIMED
            else:
                log(f"[{email}] Character {character_num}/{total_characters}: ⚠ No claim button", "WARNING")
                return FAILED

    except Exception as e:
        log(f"[{email}] Character {character_num}/{total_characters}: ✗ Error: {first_line(e)}", "ERROR")
        return FAILED

async def claim_for_account(browsers, account, account_num, total_accounts):
    """Claim jewels for ALL characters on a single account"""
    email = account['email']
    password = account['password']
    log(f"[{account_num}/{total_accounts}] Line {account['line_num']}: {email}")

    # Kept on the account so a deadline can't lose what already finished
    progress = account['progress'] = {'total': 0, 'claimed': 0, 'already': 0}

    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    safe_email = email.replace('@', '_at_').replace('.', '_')
    failure_screenshot = f"{SCREENSHOT_DIR}/{safe_email}_claim_failed.png"

    async with account_page(browsers, email) as page:
        try:
            # Login
            if not await login(page, email, password, pace=browsers.pace):
                return dict(progress)

            # Check for character selection dialog
            # Found by its text: the cookie consent dialog may sit either side of it, or be absent
            character_buttons = []

            try:
                # Get all dialogs
                all_dialogs = await page.locator('[role="dialog"]').all()

                character_dialog = None
                for dialog in all_dialogs:
                    try:
                        inner_html = await dialog.inner_html()
                        if "Select the Character" in inner_html or "Signed in as" in inner_html:
                            character_dialog = dialog
                            break
                    except PlaywrightError:
                        continue

                if character_dialog:
                    # The dialog shell can render before its profiles, and the
                    # count taken here is final: a low one never reopens the dialog
                    try:
                        await character_dialog.locator('button').first.wait_for(state="visible", timeout=5000)
                        if not await wait_for_stable_count(character_dialog.locator('button'), CARD_COUNT_STABLE_MS, CARD_COUNT_TIMEOUT_MS):
                            log(f"[{email}] Character count still changing, using the latest", "WARNING")
                    except PlaywrightTimeoutError:
                        pass

                    # Get ALL buttons from this specific dialog
                    character_buttons = await character_dialog.locator('button').all()

                    if len(character_buttons) > 0:
                        log(f"[{email}] Found {len(character_buttons)} character(s) in dialog")
            except Exception as e:
                log(f"[{email}] Error finding character dialog: {first_line(e)}", "WARNING")

            # If no character selection found, might be single character account
            if len(character_buttons) == 0:
                log(f"[{email}] No character selection found - single character account")
                # Claim for the single character
                progress['total'] = 1
                outcome = await claim_for_character(page, email, password, failure_screenshot, 1, 1)
                if outcome == CLAIMED:
                    progress['claimed'] += 1
                elif outcome == ALREADY_CLAIMED:
                    progress['already'] += 1
                return dict(progress)

            # Multiple characters found - loop through each
            total_characters = len(character_buttons)
            progress['total'] = total_characters
            log(f"[{email}] 🎮 Found {total_characters} characters!")

            for i in range(1, total_characters + 1):
                try:
                    log(f"[{email}] {'='*50}")
                    log(f"[{email}] Processing Character {i}/{total_characters}")

                    # Find and click the i-th character button
                    char_btn = None
                    try:
                        # First, check if character selection is in a dialog
                        all_dialogs = await page.locator('[role="dialog"]').all()

                        character_dialog = None
                        for dialog in all_dialogs:
                            try:
                                inner_html = await dialog.inner_html()
                                if "Select the Character" in inner_html or "Signed in as" in inner_html:
                                    character_dialog = dialog
                                    break
                            except Exception:
                                continue

                        if character_dialog:
                            # Character selection is in a dialog
                            # A reopened dialog can also render before its profiles
                            try:
                                await character_dialog.locator('button').nth(i - 1).wait_for(state="visible", timeout=5000)
                            except PlaywrightTimeoutError:
                                pass

                            buttons = await character_dialog.locator('button').all()
                            log(f"[{email}] Found {len(buttons)} character buttons in dialog")
                            if i <= len(buttons):
                                char_btn = buttons[i - 1]
                        else:
                            # Character selection might be directly in page (not in dialog)
                            page_content = await page.content()
                            if "Select the Character" in page_content:
                                log(f"[{email}] Character selection found in page (not in dialog)")

                                # Look for all buttons - filter those that look like character cards
                                # Character buttons typically contain profile info (power, level, jewels)
                                all_buttons = await page.locator('button').all()

                                # Filter buttons that contain character-specific markers
                                character_buttons_list = []
                                for btn in all_buttons:
                                    try:
                                        btn_html = await btn.inner_html()
                                        # Character buttons have power/jewels/level stats
                                        if 'Total Power' in btn_html or 'Stronghold Level' in btn_html:
                                            character_buttons_list.append(btn)
                                    except Exception:
                                        continue

                                log(f"[{email}] Found {len(character_buttons_list)} character buttons in page")
                                if i <= len(character_buttons_list):
                                    char_btn = character_buttons_list[i - 1]
                            else:
                                log(f"[{email}] No character selection found", "WARNING")

                    except Exception as e:
                        log(f"[{email}] Error finding character button: {first_line(e)}", "ERROR")

                    if not char_btn:
                        log(f"[{email}] Could not find button for character {i}", "ERROR")
                        continue

                    # Click character button
                    if await select_character(page, char_btn, email):
                        # Claim for this character
                        outcome = await claim_for_character(page, email, password, failure_screenshot, i, total_characters)
                        if outcome == CLAIMED:
                            progress['claimed'] += 1
                        elif outcome == ALREADY_CLAIMED:
                            progress['already'] += 1
                    else:
                        log(f"[{email}] Character {i}/{total_characters}: ✗ Character switch failed, skipping claim", "ERROR")

                    # If not last character, go back to character selection
                    if i < total_characters:
                        log(f"[{email}] Opening character selection...")
                        try:
                            # A failed switch can leave the dialog up, over the avatar
                            if await page.locator('text=Select the Character').locator('visible=true').first.is_visible():
                                log(f"[{email}] ✓ Character selection still open")
                                continue

                            # Step 1: Click avatar button to open dropdown menu
                            # The header depends on client session data, so it renders late
                            avatar_button = page.locator('button:has(img[src*="chat_avatar"])').locator('visible=true').first
                            try:
                                await avatar_button.wait_for(state="visible", timeout=30000)
                                await wait_interactive(avatar_button, 30000)
                                avatar_ready = True
                            except PlaywrightTimeoutError:
                                avatar_ready = False

                            if avatar_ready:
                                await avatar_button.click()

                                # Step 2: Click "Change Account" option
                                # Always in the DOM, so only visibility shows the menu opened
                                change_account_btn = page.locator('text=Change Account')
                                try:
                                    await change_account_btn.wait_for(state="visible", timeout=5000)
                                    change_account_ready = True
                                except PlaywrightTimeoutError:
                                    change_account_ready = False

                                if change_account_ready:
                                    await change_account_btn.click()

                                    # Wait for character selection to appear
                                    try:
                                        await page.wait_for_selector('text=Select the Character', timeout=7000)
                                        log(f"[{email}] ✓ Character selection opened")
                                    except Exception:
                                        log(f"[{email}] ⚠ Character selection timeout", "WARNING")
                                else:
                                    log(f"[{email}] 'Change Account' option not found", "WARNING")
                            else:
                                log(f"[{email}] Could not find avatar button", "WARNING")
                        except Exception as e:
                            log(f"[{email}] Error opening character selection: {first_line(e)}", "ERROR")

                except Exception as e:
                    log(f"[{email}] Error processing character {i}: {first_line(e)}", "ERROR")

            log(f"[{email}] {'='*50}")
            already_note = f" ({progress['already']} already claimed)" if progress['already'] > 0 else ""
            log(f"[{email}] 💎 ACCOUNT COMPLETE: Claimed {progress['claimed'] + progress['already']}/{total_characters} characters{already_note}", "SUCCESS")

            return dict(progress)

        except Exception as e:
            log(f"[{email}] ✗ Fatal error: {first_line(e)}", "ERROR")
            log_traceback(email, e)
            return dict(progress)

async def claim_account(browsers, account, account_num, total_accounts):
    """Run one account from the shared runner"""
    return await claim_for_account(browsers, account, account_num, total_accounts)

def failed_account(account):
    """Result for an account whose run raised or passed its deadline, keeping finished characters"""
    return dict(account.get('progress', {'total': 0, 'claimed': 0, 'already': 0}))

async def main():
    """Main function to process batch of accounts"""

    options, line_args = parse_run_options(sys.argv[1:], USAGE, allow_lines=True)

    if options.lines is not None:
        if line_args:
            print("Error: --lines cannot be combined with a line range")
            sys.exit(1)

        start_line = options.lines[0]
        end_line = options.lines[-1]
    # If no arguments provided, process all accounts
    elif not line_args:
        # Count total lines in users.txt
        if not os.path.exists(USERS_FILE):
            print(f"Error: Users file not found: {USERS_FILE}")
            sys.exit(1)

        with open(USERS_FILE, 'r') as f:
            total_lines = sum(1 for _ in f)

        start_line = 1
        end_line = total_lines
        say(f"No range specified - processing all {total_lines} accounts")
    elif len(line_args) == 2:
        try:
            start_line = int(line_args[0])
            end_line = int(line_args[1])
        except ValueError:
            print("Error: start_line and end_line must be integers")
            sys.exit(1)
    else:
        print(USAGE)
        sys.exit(1)

    log("=" * 60)
    log(f"Game of War - Multi-Character Claim (Lines {start_line}-{end_line})")
    log("=" * 60)

    accounts = read_accounts(start_line, end_line)

    if options.lines is not None:
        found = {account['line_num'] for account in accounts}
        for line in options.lines:
            if line not in found:
                log(f"Line {line} has no valid account", "WARNING")

        accounts = [account for account in accounts if account['line_num'] in options.lines]

    if not accounts:
        log(f"No valid accounts found in range {start_line}-{end_line}", "ERROR")
        return False

    log(f"Found {len(accounts)} account(s) to process")
    log("-" * 60)

    # Process the accounts in parallel; outcomes come back in input order
    outcomes = await run_accounts(accounts, claim_account, failed_account, options)

    results = []
    total_characters_processed = 0
    total_already_claimed = 0
    total_jewels_claimed = 0

    for account, result in zip(accounts, outcomes):
        results.append({
            'email': account['email'],
            'line_num': account['line_num'],
            'characters': result['total'],
            'claimed': result['claimed'],
            'already': result['already'],
            'success': result['claimed'] + result['already'] > 0 and not account.get('timed_out')
        })

        total_characters_processed += result['total']
        total_already_claimed += result['already']
        total_jewels_claimed += result['claimed'] * JEWELS_PER_CLAIM

    # Summary
    log("=" * 60)
    log("MULTI-CHARACTER BATCH SUMMARY")
    log("=" * 60)

    successful_accounts = sum(1 for r in results if r['success'])
    failed_accounts = sum(1 for r in results if not r['success'])

    for result in results:
        if result['success']:
            status = f"✅ {result['claimed'] + result['already']}/{result['characters']} chars"
        else:
            status = "❌ FAILED"
        log(f"{status} (Line {result['line_num']}): {result['email']}")

    log("-" * 60)
    log(f"Accounts Processed: {len(results)}")
    log(f"Accounts Success: {successful_accounts}")
    log(f"Accounts Failed: {failed_accounts}")
    log(f"Total Characters Found: {total_characters_processed}")
    log(f"Characters Already Claimed: {total_already_claimed}")
    log(f"Total Jewels Claimed: {total_jewels_claimed} 💎")
    log("=" * 60)

    return failed_accounts == 0

if __name__ == "__main__":
    try:
        success = asyncio.run(exit_if_stuck(main()))
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        log("Interrupted by user", "WARNING")
        sys.exit(1)
    except Exception as e:
        log(f"Fatal error: {first_line(e)}", "ERROR")
        sys.exit(1)
