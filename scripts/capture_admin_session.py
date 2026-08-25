from __future__ import annotations

import asyncio

from app.config import load_settings


async def capture() -> None:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise SystemExit(
            "Install dependencies and Chromium first: "
            "pip install -r requirements.txt && "
            "python -m playwright install chromium"
        ) from exc

    settings = load_settings()
    target = settings.admin_auth_state_file
    target.parent.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context_options = {"viewport": {"width": 1440, "height": 1000}}
        if target.is_file():
            context_options["storage_state"] = str(target)
        context = await browser.new_context(**context_options)
        page = await context.new_page()
        await page.goto(settings.admin_base_url, wait_until="domcontentloaded")
        await asyncio.to_thread(
            input,
            "Log in to the Nedra admin page in the opened browser, then press "
            "Enter here to save the session: ",
        )
        await context.storage_state(path=str(target), indexed_db=True)
        await context.close()
        await browser.close()

    print(f"Admin session saved to {target}")
    print("Keep this file secret: it grants access to the admin account.")


def main() -> None:
    asyncio.run(capture())


if __name__ == "__main__":
    main()
