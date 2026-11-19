"""Live Playwright Chromium Instagram Web session manager, profile analyzer, and post/draft uploader."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from smai.config import Settings


def discover_chromium_executable() -> str | None:
    """Find cached Playwright Chromium or system Chrome/Edge on Windows."""
    local_app = os.environ.get("LOCALAPPDATA", "")
    if local_app:
        pw_root = Path(local_app) / "ms-playwright"
        if pw_root.exists():
            for pattern in (
                "chromium-*/chrome-win64/chrome.exe",
                "chromium_headless_shell-*/chrome-headless-shell-win64/chrome-headless-shell.exe",
            ):
                matches = sorted(pw_root.glob(pattern), reverse=True)
                if matches:
                    return str(matches[0])
    for sys_browser in (
        "C:/Program Files/Google/Chrome/Application/chrome.exe",
        "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
    ):
        if Path(sys_browser).exists():
            return sys_browser
    return None


class InstagramBrowserAutomator:
    """
    Connects to the user's real Instagram account using a persistent local Chromium profile
    (data/ig_browser_profile). Supports:
    1) Interactive 1-time login (user logs in visibly in Chromium; session cookies persist on disk)
    2) Live profile & post scraping from the authenticated session
    3) Live uploading of Reels (.mp4), Carousels (.pngs), and Single Pics into Instagram's Create modal,
       filling the caption, and either pausing on the final 'Share' screen (Draft/Review mode) or clicking Share.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.profile_dir = settings.resolve_data_dir() / "ig_browser_profile"
        self.profile_dir.mkdir(parents=True, exist_ok=True)

    async def launch_login_window(self, wait_seconds: int = 90) -> dict[str, Any]:
        """
        Open a visible Chromium window at instagram.com so the user can log in once.
        Saves session state automatically inside data/ig_browser_profile.
        """
        from playwright.async_api import async_playwright

        exe = discover_chromium_executable()
        async with async_playwright() as p:
            launch_kwargs: dict[str, Any] = {
                "user_data_dir": str(self.profile_dir),
                "headless": False,
                "viewport": {"width": 1280, "height": 900},
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            if exe:
                launch_kwargs["executable_path"] = exe

            context = await p.chromium.launch_persistent_context(**launch_kwargs)
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=45000)

                # Poll up to wait_seconds for authenticated session cookie 'ds_user_id'
                logged_in = False
                ds_user_id = ""
                for _ in range(max(wait_seconds // 2, 5)):
                    cookies = await context.cookies("https://www.instagram.com")
                    for c in cookies:
                        if c.get("name") == "ds_user_id" and c.get("value"):
                            logged_in = True
                            ds_user_id = str(c["value"])
                            break
                    if logged_in:
                        # Give 3 extra seconds for the home feed to settle
                        await asyncio.sleep(3.0)
                        break
                    await asyncio.sleep(2.0)

                return {
                    "logged_in": logged_in,
                    "ds_user_id": ds_user_id,
                    "profile_dir": str(self.profile_dir),
                }
            finally:
                await context.close()

    def has_saved_session(self) -> bool:
        """Check whether the persistent Chromium profile directory has cookies/state."""
        cookies_db = self.profile_dir / "Default" / "Network" / "Cookies"
        alt_cookies = self.profile_dir / "Default" / "Cookies"
        return cookies_db.exists() or alt_cookies.exists()

    async def upload_draft_to_instagram(
        self,
        media_paths: list[Path],
        caption: str,
        auto_share: bool = False,
        hold_open_seconds: int = 45,
    ) -> dict[str, Any]:
        """
        Open Instagram Web in a visible Chromium window using the saved session,
        click Create (+), upload the media files (.mp4 or .pngs), advance through
        Crop -> Filter -> Caption, fill the caption, and either click 'Share'
        (if auto_share=True) or leave the composer open for hold_open_seconds so the
        user can review and click Share themselves.
        """
        from playwright.async_api import async_playwright

        valid_files = [str(p.resolve()) for p in media_paths if p.exists() and p.stat().st_size > 0]
        if not valid_files:
            raise ValueError("No valid media files found to upload to Instagram.")

        exe = discover_chromium_executable()
        async with async_playwright() as p:
            launch_kwargs: dict[str, Any] = {
                "user_data_dir": str(self.profile_dir),
                "headless": False,
                "viewport": {"width": 1280, "height": 920},
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            if exe:
                launch_kwargs["executable_path"] = exe

            context = await p.chromium.launch_persistent_context(**launch_kwargs)
            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto("https://www.instagram.com/", wait_until="domcontentloaded", timeout=45000)
                await asyncio.sleep(3.0)

                # Dismiss optional 'Turn on Notifications' dialog if present
                for dismiss_text in ("Not Now", "Not now"):
                    try:
                        btn = page.get_by_role("button", name=dismiss_text)
                        if await btn.count() > 0:
                            await btn.first.click(timeout=2000)
                    except Exception:
                        pass

                # 1. Click the 'New post' / 'Create' button in the left sidebar
                clicked_create = False
                for selector in (
                    'svg[aria-label="New post"]',
                    'a:has-text("Create")',
                    'span:has-text("Create")',
                ):
                    try:
                        loc = page.locator(selector).first
                        if await loc.count() > 0:
                            await loc.click(timeout=4000)
                            clicked_create = True
                            break
                    except Exception:
                        continue

                await asyncio.sleep(1.5)

                # If a submenu 'Post' appears under Create, click it
                try:
                    post_sub = page.locator('span:has-text("Post")').first
                    if await post_sub.is_visible():
                        await post_sub.click(timeout=2000)
                except Exception:
                    pass

                await asyncio.sleep(1.5)

                # 2. Attach files to the <input type="file"> element inside the Create modal
                file_input = page.locator('input[type="file"]').first
                await file_input.set_input_files(valid_files, timeout=15000)
                await asyncio.sleep(3.0)

                # Dismiss 'Video posts are now shared as reels' OK popup if shown
                try:
                    ok_btn = page.get_by_role("button", name="OK")
                    if await ok_btn.count() > 0:
                        await ok_btn.first.click(timeout=2000)
                except Exception:
                    pass

                # 3. Click 'Next' twice (Crop step -> Filter/Edit step -> Caption step)
                for _ in range(2):
                    await asyncio.sleep(2.0)
                    try:
                        next_btn = page.get_by_role("button", name="Next").first
                        await next_btn.click(timeout=8000)
                    except Exception:
                        pass

                await asyncio.sleep(2.0)

                # 4. Fill Caption in the caption textbox
                caption_filled = False
                for cap_selector in (
                    'div[aria-label="Write a caption..."]',
                    'div[role="textbox"]',
                ):
                    try:
                        box = page.locator(cap_selector).first
                        if await box.count() > 0:
                            await box.click(timeout=4000)
                            await box.fill(caption[:2150])
                            caption_filled = True
                            break
                    except Exception:
                        continue

                shared = False
                if auto_share:
                    try:
                        share_btn = page.get_by_role("button", name="Share").first
                        await share_btn.click(timeout=8000)
                        shared = True
                        await asyncio.sleep(8.0)
                    except Exception:
                        pass
                else:
                    # Keep window open so the user can inspect the staged post/draft and click Share
                    await asyncio.sleep(hold_open_seconds)

                return {
                    "mode": "ig_browser",
                    "clicked_create": clicked_create,
                    "files_uploaded": valid_files,
                    "caption_filled": caption_filled,
                    "auto_shared": shared,
                    "status": (
                        "confirmed_shared"
                        if (auto_share and shared)
                        else "staged_awaiting_confirmation"
                    ),
                }
            finally:
                await context.close()
