#!/usr/bin/env python3
"""
Game of War - Multi-Character Batch Jewel Claim
Processes a range of accounts and claims for ALL characters on each account
"""

import asyncio
from playwright.async_api import async_playwright
from datetime import datetime
import sys
import os

USERS_FILE = "/Users/johnalvero/godofwar/users.txt"
SCREENSHOT_DIR = "/Users/johnalvero/godofwar/screenshots"

def log(msg, level="INFO"):
    """Print timestamped log message"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] [{level}] {msg}")

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
            log(f"Invalid format on line {line_num + 1}: {line}", "WARNING")
            continue

        parts = line.split(':', 1)
        if len(parts) != 2:
            continue

        email, password = parts
        email = email.strip()
        password = password.strip()

        if email and password:
            accounts.append({
                'email': email,
                'password': password,
                'line_num': line_num + 1
            })

    return accounts

async def claim_for_character(page, email, character_num, total_characters):
    """Claim jewels for a specific character (already selected)"""
    try:
        # Navigate to vault
        log(f"[{email}] Character {character_num}/{total_characters}: Navigating to vault...")
        await page.goto("https://www.gameofwar-fireage.com/en-us/catalog/main", wait_until="networkidle", timeout=60000)
        await page.wait_for_timeout(2000)

        # Try to claim
        claim_button = page.locator('button:has-text("Claim Now!")')
        button_count = await claim_button.count()

        if button_count > 0:
            log(f"[{email}] Character {character_num}/{total_characters}: Found Claim button, clicking...")
            await claim_button.click()
            await page.wait_for_timeout(3000)

            # Verify claim succeeded
            page_text = await page.content()
            if "Available again at" in page_text:
                log(f"[{email}] Character {character_num}/{total_characters}: ✅ Claimed 75 jewels!", "SUCCESS")
                return True
            else:
                log(f"[{email}] Character {character_num}/{total_characters}: ⚠ Claim clicked but unclear", "WARNING")
                return True
        else:
            # Check if already on cooldown
            page_text = await page.content()
            if "Available again at" in page_text or "remaining" in page_text.lower():
                log(f"[{email}] Character {character_num}/{total_characters}: ✓ Already claimed", "SUCCESS")
                return True
            else:
                log(f"[{email}] Character {character_num}/{total_characters}: ⚠ No claim button", "WARNING")
                return False

    except Exception as e:
        log(f"[{email}] Character {character_num}/{total_characters}: ✗ Error: {e}", "ERROR")
        return False

async def claim_for_account(email, password, account_num, total_accounts, line_num):
    """Claim jewels for ALL characters on a single account"""
    log(f"[{account_num}/{total_accounts}] Line {line_num}: {email}")

    os.makedirs(SCREENSHOT_DIR, exist_ok=True)
    safe_email = email.replace('@', '_at_').replace('.', '_')

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-setuid-sandbox']
        )

        page = await browser.new_page()

        try:
            # Login
            log(f"[{email}] Loading login page...")
            response = await page.goto("https://www.gameofwar-fireage.com/en-us/login", timeout=60000)

            if not response or not response.ok:
                log(f"[{email}] Failed to load page", "ERROR")
                return {'total': 0, 'claimed': 0}

            await page.wait_for_load_state("networkidle", timeout=30000)

            # Fill login form
            log(f"[{email}] Filling credentials...")
            await page.locator('input').first.fill(email)
            await page.locator('input').nth(1).fill(password)

            # Dismiss cookie consent
            try:
                cookie_buttons = ['button:has-text("Accept")', '#onetrust-accept-btn-handler']
                for selector in cookie_buttons:
                    try:
                        btn = page.locator(selector)
                        if await btn.count() > 0:
                            await btn.first.click(timeout=2000)
                            await page.wait_for_timeout(1000)
                            break
                    except:
                        continue
            except:
                pass

            # Login
            log(f"[{email}] Logging in...")
            await page.locator('button:has-text("Login")').click()
            await page.wait_for_timeout(6000)

            # Check for character selection dialog
            # The character dialog is the SECOND dialog (first is cookie consent)
            character_buttons = []

            try:
                # Get all dialogs
                all_dialogs = await page.locator('[role="dialog"]').all()

                if len(all_dialogs) >= 2:
                    # Character dialog is usually the second one (index 1)
                    character_dialog = all_dialogs[1]

                    # Get ALL buttons from this specific dialog
                    character_buttons = await character_dialog.locator('button').all()

                    if len(character_buttons) > 0:
                        log(f"[{email}] Found {len(character_buttons)} character(s) in dialog")
                elif len(all_dialogs) == 1:
                    # Only one dialog, check if it's the character dialog
                    dialog = all_dialogs[0]
                    inner_html = await dialog.inner_html()
                    if "Select the Character" in inner_html or "Signed in as" in inner_html:
                        character_buttons = await dialog.locator('button').all()
                        log(f"[{email}] Found {len(character_buttons)} character(s) in single dialog")
            except Exception as e:
                log(f"[{email}] Error finding character dialog: {e}", "WARNING")

            # If no character selection found, might be single character account
            if len(character_buttons) == 0:
                log(f"[{email}] No character selection found - single character account")
                # Claim for the single character
                success = await claim_for_character(page, email, 1, 1)
                return {'total': 1, 'claimed': 1 if success else 0}

            # Multiple characters found - loop through each
            total_characters = len(character_buttons)
            log(f"[{email}] 🎮 Found {total_characters} characters!")

            claimed_count = 0
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
                            except:
                                continue

                        if character_dialog:
                            # Character selection is in a dialog
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
                                    except:
                                        continue

                                log(f"[{email}] Found {len(character_buttons_list)} character buttons in page")
                                if i <= len(character_buttons_list):
                                    char_btn = character_buttons_list[i - 1]
                            else:
                                log(f"[{email}] No character selection found", "WARNING")

                    except Exception as e:
                        log(f"[{email}] Error finding character button: {e}", "ERROR")

                    if not char_btn:
                        log(f"[{email}] Could not find button for character {i}", "ERROR")
                        continue

                    # Click character button
                    await char_btn.click()
                    await page.wait_for_timeout(3000)

                    # Claim for this character
                    success = await claim_for_character(page, email, i, total_characters)
                    if success:
                        claimed_count += 1

                    # If not last character, go back to character selection
                    if i < total_characters:
                        log(f"[{email}] Opening character selection...")
                        try:
                            # Step 1: Click avatar button to open dropdown menu
                            avatar_buttons = await page.locator('button:has(img[src*="chat_avatar"])').all()
                            if len(avatar_buttons) > 0:
                                await avatar_buttons[0].click()
                                await page.wait_for_timeout(1000)

                                # Step 2: Click "Change Account" option
                                change_account_btn = page.locator('text=Change Account')
                                if await change_account_btn.count() > 0:
                                    await change_account_btn.click()
                                    await page.wait_for_timeout(2000)

                                    # Wait for character selection to appear
                                    try:
                                        await page.wait_for_selector('text=Select the Character', timeout=5000)
                                        log(f"[{email}] ✓ Character selection opened")
                                    except:
                                        log(f"[{email}] ⚠ Character selection timeout", "WARNING")
                                else:
                                    log(f"[{email}] 'Change Account' option not found", "WARNING")
                            else:
                                log(f"[{email}] Could not find avatar button", "WARNING")
                        except Exception as e:
                            log(f"[{email}] Error opening character selection: {e}", "ERROR")

                except Exception as e:
                    log(f"[{email}] Error processing character {i}: {e}", "ERROR")

            log(f"[{email}] {'='*50}")
            log(f"[{email}] 💎 ACCOUNT COMPLETE: Claimed {claimed_count}/{total_characters} characters", "SUCCESS")

            return {'total': total_characters, 'claimed': claimed_count}

        except Exception as e:
            log(f"[{email}] ✗ Fatal error: {e}", "ERROR")
            import traceback
            traceback.print_exc()
            return {'total': 0, 'claimed': 0}

        finally:
            await browser.close()

async def main():
    """Main function to process batch of accounts"""

    # If no arguments provided, process all accounts
    if len(sys.argv) == 1:
        # Count total lines in users.txt
        if not os.path.exists(USERS_FILE):
            print(f"Error: Users file not found: {USERS_FILE}")
            sys.exit(1)

        with open(USERS_FILE, 'r') as f:
            total_lines = sum(1 for _ in f)

        start_line = 1
        end_line = total_lines
        print(f"No range specified - processing all {total_lines} accounts")
    elif len(sys.argv) == 3:
        try:
            start_line = int(sys.argv[1])
            end_line = int(sys.argv[2])
        except ValueError:
            print("Error: start_line and end_line must be integers")
            sys.exit(1)
    else:
        print("Usage: python claim_all_characters.py [<start_line> <end_line>]")
        print("Examples:")
        print("  python claim_all_characters.py           # Process all accounts")
        print("  python claim_all_characters.py 1 50      # Process accounts 1-50")
        sys.exit(1)

    log("=" * 60)
    log(f"Game of War - Multi-Character Claim (Lines {start_line}-{end_line})")
    log("=" * 60)

    accounts = read_accounts(start_line, end_line)

    if not accounts:
        log(f"No valid accounts found in range {start_line}-{end_line}", "ERROR")
        return False

    log(f"Found {len(accounts)} account(s) to process")
    log("-" * 60)

    # Process each account
    results = []
    total_characters_processed = 0
    total_jewels_claimed = 0

    for i, account in enumerate(accounts, 1):
        try:
            result = await claim_for_account(
                account['email'],
                account['password'],
                i,
                len(accounts),
                account['line_num']
            )

            results.append({
                'email': account['email'],
                'line_num': account['line_num'],
                'characters': result['total'],
                'claimed': result['claimed'],
                'success': result['claimed'] > 0
            })

            total_characters_processed += result['total']
            total_jewels_claimed += result['claimed'] * 75

            # Wait between accounts
            if i < len(accounts):
                log(f"Waiting 10 seconds before next account...")
                await asyncio.sleep(10)

        except Exception as e:
            log(f"Error processing {account['email']}: {e}", "ERROR")
            results.append({
                'email': account['email'],
                'line_num': account['line_num'],
                'characters': 0,
                'claimed': 0,
                'success': False
            })

    # Summary
    log("=" * 60)
    log("MULTI-CHARACTER BATCH SUMMARY")
    log("=" * 60)

    successful_accounts = sum(1 for r in results if r['success'])
    failed_accounts = sum(1 for r in results if not r['success'])

    for result in results:
        if result['success']:
            status = f"✅ {result['claimed']}/{result['characters']} chars"
        else:
            status = "❌ FAILED"
        log(f"{status} (Line {result['line_num']}): {result['email']}")

    log("-" * 60)
    log(f"Accounts Processed: {len(results)}")
    log(f"Accounts Success: {successful_accounts}")
    log(f"Accounts Failed: {failed_accounts}")
    log(f"Total Characters Found: {total_characters_processed}")
    log(f"Total Jewels Claimed: {total_jewels_claimed} 💎")
    log("=" * 60)

    return failed_accounts == 0

if __name__ == "__main__":
    try:
        success = asyncio.run(main())
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        log("Interrupted by user", "WARNING")
        sys.exit(1)
    except Exception as e:
        log(f"Fatal error: {e}", "ERROR")
        sys.exit(1)
