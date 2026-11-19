#!/usr/bin/env python3
"""
Debug script to check what's happening with the Game of War login page
"""

import asyncio
from playwright.async_api import async_playwright

async def debug():
    async with async_playwright() as p:
        # Launch browser in VISIBLE mode to see what's happening
        browser = await p.chromium.launch(headless=False)

        context = await browser.new_context(
            viewport={'width': 1920, 'height': 1080}
        )

        page = await context.new_page()

        print("Navigating to login page...")
        await page.goto("https://www.gameofwar-fireage.com/en-us/login")

        print("Waiting 5 seconds to see page...")
        await page.wait_for_timeout(5000)

        # Take screenshot
        await page.screenshot(path='/Users/johnalvero/Apps/godofwar/debug_screenshot.png')
        print("Screenshot saved to: debug_screenshot.png")

        # Get page HTML
        html = await page.content()
        with open('/Users/johnalvero/Apps/godofwar/debug_page.html', 'w') as f:
            f.write(html)
        print("HTML saved to: debug_page.html")

        # Try to find email field
        print("\nLooking for email field...")
        email_selectors = [
            'input[type="email"]',
            'input',
            'textbox',
            '[role="textbox"]'
        ]

        for selector in email_selectors:
            try:
                elements = await page.query_selector_all(selector)
                if elements:
                    print(f"✓ Found {len(elements)} elements with: {selector}")
                else:
                    print(f"✗ No elements with: {selector}")
            except Exception as e:
                print(f"✗ Error with {selector}: {e}")

        print("\nPress Ctrl+C to close browser...")
        await page.wait_for_timeout(30000)

        await browser.close()

asyncio.run(debug())
