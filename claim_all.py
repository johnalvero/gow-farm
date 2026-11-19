#!/usr/bin/env python3
"""
Game of War - Multi-Account Jewel Claim
Reads accounts from users.txt and claims jewels for each one
"""

import asyncio
import sys
import os

from claim_common import (
    ALREADY_CLAIMED,
    CLAIMED,
    FAILED,
    JEWELS_PER_CLAIM,
    account_page,
    exit_if_stuck,
    first_line,
    is_valid_email_field,
    log,
    log_traceback,
    login,
    parse_run_options,
    run_accounts,
    save_screenshot,
    submit_claim,
    vault_state,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.environ.get("GOW_USERS_FILE", os.path.join(BASE_DIR, "users.txt"))
SCREENSHOT_DIR = os.path.join(BASE_DIR, "screenshots")

USAGE = "Usage: python claim_all.py [--concurrency N] [--stagger SECONDS] [--no-block] [--force-concurrency] [--no-dashboard]"

def read_accounts():
    """Read accounts from users.txt file"""
    accounts = []

    if not os.path.exists(USERS_FILE):
        log(f"Users file not found: {USERS_FILE}", "ERROR")
        return accounts

    with open(USERS_FILE, 'r') as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()

            # Skip empty lines and comments
            if not line or line.startswith('#'):
                continue

            # Parse email:password format
            if ':' not in line:
                log(f"Invalid format on line {line_num}", "WARNING")
                continue

            email, password = line.split(':', 1)
            email = email.strip()
            if not is_valid_email_field(email):
                log(f"Invalid format on line {line_num}", "WARNING")
                continue

            password = password.strip()

            if email and password:
                accounts.append({'email': email, 'password': password, 'line_num': line_num})
            else:
                log(f"Empty email or password on line {line_num}", "WARNING")

    return accounts

async def claim_for_account(browsers, account, account_num, total_accounts):
    """Claim jewels for a single account"""
    email = account['email']
    password = account['password']
    log(f"[{account_num}/{total_accounts}] Processing account: {email}")

    # Create screenshots directory if needed
    os.makedirs(SCREENSHOT_DIR, exist_ok=True)

    # Sanitize email for filename
    safe_email = email.replace('@', '_at_').replace('.', '_')
    failure_screenshot = f"{SCREENSHOT_DIR}/{safe_email}_claim_failed.png"

    async with account_page(browsers, email) as page:
        try:
            # Log in
            if not await login(page, email, password, pace=browsers.pace):
                return FAILED

            current_url = page.url
            log(f"[{email}] After login URL: {current_url}")

            # Handle character selection
            if 'catalog' not in current_url and 'login' not in current_url:
                log(f"[{email}] Selecting character...")
                await page.wait_for_timeout(2000)

                try:
                    buttons = await page.locator('button[role="button"]').all()
                    if len(buttons) > 0:
                        await buttons[0].click()
                        log(f"[{email}] Selected character")
                        await page.wait_for_timeout(3000)
                except Exception:
                    log(f"[{email}] Character selection skipped", "WARNING")

            # Navigate to vault
            log(f"[{email}] Navigating to vault...")
            state = await vault_state(page, email)

            # Save screenshot
            screenshot_path = f"{SCREENSHOT_DIR}/{safe_email}_catalog.png"
            await save_screenshot(page, screenshot_path, email)

            # Try to claim first
            log(f"[{email}] Looking for Claim button...")
            try:
                if state == "claim":
                    log(f"[{email}] Found Claim button, clicking...")

                    # Only the server's response counts as a claim
                    if await submit_claim(page, email, password, failure_screenshot):
                        # On the account so a deadline can't lose it
                        account['outcome'] = CLAIMED

                        screenshot_path = f"{SCREENSHOT_DIR}/{safe_email}_after_claim.png"
                        await save_screenshot(page, screenshot_path, email)

                        log(f"[{email}] ✅ Successfully claimed 75 jewels!", "SUCCESS")
                        return CLAIMED
                    else:
                        log(f"[{email}] ✗ Claim failed", "ERROR")
                        return FAILED
                else:
                    # No claim button - check if already on cooldown
                    if state == "cooldown":
                        account['outcome'] = ALREADY_CLAIMED
                        log(f"[{email}] ✓ Jewels already claimed today (on cooldown)", "SUCCESS")
                        return ALREADY_CLAIMED
                    else:
                        log(f"[{email}] ⚠ Claim button not found", "WARNING")
                        return FAILED

            except Exception as e:
                log(f"[{email}] ✗ Error clicking claim: {first_line(e)}", "ERROR")
                return FAILED

        except Exception as e:
            log(f"[{email}] ✗ Fatal error: {first_line(e)}", "ERROR")
            log_traceback(email, e)
            return FAILED

async def claim_account(browsers, account, account_num, total_accounts):
    """Run one account from the shared runner"""
    return await claim_for_account(browsers, account, account_num, total_accounts)

def failed_account(account):
    """Result for an account whose run raised or passed its deadline, keeping a finished claim"""
    return account.get('outcome', FAILED)

async def main():
    """Main function to process all accounts"""
    options, leftovers = parse_run_options(sys.argv[1:], USAGE)

    # This script takes no positionals, so a stray dash arg is a mistyped flag
    for arg in leftovers:
        if arg == "-h":
            print(USAGE)
            sys.exit(0)
        if arg.startswith("-"):
            print(f"Error: unknown option {arg.split('=', 1)[0]}")
            sys.exit(1)

    log("=" * 60)
    log("Game of War - Multi-Account Jewel Claim")
    log("=" * 60)

    # Read accounts
    accounts = read_accounts()

    if not accounts:
        log("No accounts found in users.txt", "ERROR")
        log(f"Please add accounts to: {USERS_FILE}", "ERROR")
        log("Format: email:password (one per line)", "ERROR")
        return False

    log(f"Found {len(accounts)} account(s) to process")
    log("-" * 60)

    # Process the accounts in parallel; outcomes come back in input order
    outcomes = await run_accounts(accounts, claim_account, failed_account, options)

    results = []
    for account, outcome in zip(accounts, outcomes):
        results.append({
            'email': account['email'],
            'outcome': outcome,
            'success': outcome != FAILED and not account.get('timed_out')
        })

    # Summary
    log("=" * 60)
    log("SUMMARY")
    log("=" * 60)

    successful = sum(1 for r in results if r['success'])
    failed = sum(1 for r in results if not r['success'])
    new_claims = sum(1 for r in results if r['outcome'] == CLAIMED)
    already_claimed = sum(1 for r in results if r['outcome'] == ALREADY_CLAIMED)

    for result in results:
        status = "✅ SUCCESS" if result['success'] else "❌ FAILED"
        log(f"{status}: {result['email']}")

    log("-" * 60)
    log(f"Total: {len(results)} | Success: {successful} | Failed: {failed}")
    log(f"Characters Already Claimed: {already_claimed}")
    log(f"Total Jewels Claimed: {new_claims * JEWELS_PER_CLAIM} 💎")
    log("=" * 60)

    return failed == 0

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
