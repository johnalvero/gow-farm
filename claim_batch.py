#!/usr/bin/env python3
"""
Game of War - Batch Jewel Claim
Processes a specific range of accounts from users.txt
Usage: python claim_batch.py <start_line> <end_line> [--concurrency N] [--stagger SECONDS] [--no-block] [--force-concurrency]
Example: python claim_batch.py 1 50  (processes accounts 1-50)
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
    login,
    parse_run_options,
    run_accounts,
    submit_claim,
    vault_state,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
USERS_FILE = os.environ.get("GOW_USERS_FILE", os.path.join(BASE_DIR, "users.txt"))
SCREENSHOT_DIR = os.path.join(BASE_DIR, "screenshots")

USAGE = """Usage: python claim_batch.py <start_line> <end_line> [--concurrency N] [--stagger SECONDS] [--no-block] [--force-concurrency] [--no-dashboard]
Example: python claim_batch.py 1 50"""

def read_accounts(start_line, end_line):
    """Read accounts from users.txt within the specified range"""
    accounts = []

    if not os.path.exists(USERS_FILE):
        log(f"Users file not found: {USERS_FILE}", "ERROR")
        return accounts

    with open(USERS_FILE, 'r') as f:
        lines = f.readlines()

    # Adjust for 1-based indexing
    start_idx = start_line - 1
    end_idx = min(end_line, len(lines))

    for line_num in range(start_idx, end_idx):
        line = lines[line_num].strip()

        # Skip empty lines and comments
        if not line or line.startswith('#'):
            continue

        # Parse email:password format
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

async def claim_for_account(browsers, account, account_num, total_accounts):
    """Claim jewels for a single account"""
    email = account['email']
    password = account['password']
    log(f"[{account_num}/{total_accounts}] Line {account['line_num']}: {email}")

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

            # Handle character selection
            current_url = page.url
            if 'catalog' not in current_url and 'login' not in current_url:
                await page.wait_for_timeout(2000)
                try:
                    buttons = await page.locator('button[role="button"]').all()
                    if len(buttons) > 0:
                        await buttons[0].click()
                        await page.wait_for_timeout(3000)
                except Exception:
                    pass

            # Navigate to vault
            log(f"[{email}] Navigating to vault...")
            state = await vault_state(page, email)

            # Try to claim first
            try:
                if state == "claim":
                    log(f"[{email}] Found Claim button, clicking...")

                    # Only the server's response counts as a claim
                    if await submit_claim(page, email, password, failure_screenshot):
                        # On the account so a deadline can't lose it
                        account['outcome'] = CLAIMED
                        log(f"[{email}] ✅ Successfully claimed 75 jewels!", "SUCCESS")
                        return CLAIMED
                    else:
                        log(f"[{email}] ✗ Claim failed", "ERROR")
                        return FAILED
                else:
                    # No claim button - check if already on cooldown
                    if state == "cooldown":
                        account['outcome'] = ALREADY_CLAIMED
                        log(f"[{email}] ✓ Already claimed (on cooldown)", "SUCCESS")
                        return ALREADY_CLAIMED
                    else:
                        log(f"[{email}] ⚠ No claim button found", "WARNING")
                        return FAILED

            except Exception as e:
                log(f"[{email}] ✗ Claim error: {first_line(e)}", "ERROR")
                return FAILED

        except Exception as e:
            log(f"[{email}] ✗ Fatal error: {first_line(e)}", "ERROR")
            return FAILED

async def claim_account(browsers, account, account_num, total_accounts):
    """Run one account from the shared runner"""
    return await claim_for_account(browsers, account, account_num, total_accounts)

def failed_account(account):
    """Result for an account whose run raised or passed its deadline, keeping a finished claim"""
    return account.get('outcome', FAILED)

async def main():
    """Main function to process batch of accounts"""

    options, line_args = parse_run_options(sys.argv[1:], USAGE)

    # Parse command line arguments
    if len(line_args) != 2:
        print(USAGE)
        sys.exit(1)

    try:
        start_line = int(line_args[0])
        end_line = int(line_args[1])
    except ValueError:
        print("Error: start_line and end_line must be integers")
        sys.exit(1)

    if start_line < 1 or end_line < start_line:
        print("Error: Invalid line range")
        sys.exit(1)

    log("=" * 60)
    log(f"Game of War - Batch Jewel Claim (Lines {start_line}-{end_line})")
    log("=" * 60)

    # Read accounts
    accounts = read_accounts(start_line, end_line)

    if not accounts:
        log(f"No valid accounts found in range {start_line}-{end_line}", "ERROR")
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
            'success': outcome != FAILED and not account.get('timed_out'),
            'line_num': account['line_num']
        })

    # Summary
    log("=" * 60)
    log("BATCH SUMMARY")
    log("=" * 60)

    successful = sum(1 for r in results if r['success'])
    failed = sum(1 for r in results if not r['success'])
    new_claims = sum(1 for r in results if r['outcome'] == CLAIMED)
    already_claimed = sum(1 for r in results if r['outcome'] == ALREADY_CLAIMED)

    for result in results:
        status = "✅ SUCCESS" if result['success'] else "❌ FAILED"
        log(f"{status} (Line {result['line_num']}): {result['email']}")

    log("-" * 60)
    log(f"Range: Lines {start_line}-{end_line}")
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
