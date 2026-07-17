#!/usr/bin/env python3
"""Smoke-check the NEW web app at /hokku/static/app/index.html.

Assumes a server is already running on 127.0.0.1:8080 (see docstring in
tools/verify_ui.py for the launch pattern). Grows one assertion block per
milestone; keep failures loud and specific.

Usage:  python tools/check_app.py [--mobile]
"""

import asyncio
import sys

from playwright.async_api import async_playwright

sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252

BASE = "http://127.0.0.1:8080"
APP = f"{BASE}/hokku/static/app/index.html"

MOBILE = "--mobile" in sys.argv
VIEWPORT = {"width": 390, "height": 844} if MOBILE else {"width": 1380, "height": 900}


async def main() -> int:
    failures: list[str] = []
    console_errors: list[str] = []

    async with async_playwright() as p:
        # Prefer the downloaded chromium; fall back to system Edge/Chrome so the
        # check runs even when the playwright browser download is unavailable.
        browser = None
        for channel in (None, "msedge", "chrome"):
            try:
                browser = await p.chromium.launch(channel=channel) if channel else await p.chromium.launch()
                break
            except Exception:
                continue
        if browser is None:
            print("FAIL: no usable browser (run: playwright install chromium)")
            return 1
        page = await browser.new_page(viewport=VIEWPORT, has_touch=MOBILE, is_mobile=MOBILE)
        # Resource 404s (e.g. a thumbnail that can't be built for a broken/converting
        # image) are handled by <img onerror> and are not app errors — ignore them here;
        # real JS faults arrive as pageerror or non-resource console errors.
        page.on(
            "console",
            lambda m: console_errors.append(m.text)
            if (m.type == "error" and "Failed to load resource" not in m.text)
            else None,
        )
        page.on("pageerror", lambda e: console_errors.append(str(e)))

        print(f"Opening {APP} ({VIEWPORT['width']}x{VIEWPORT['height']})...")
        await page.goto(APP, timeout=15000)
        await page.wait_for_load_state("networkidle", timeout=10000)

        # ── M0: chrome is alive ────────────────────────────────────────────
        meta = await page.locator("#meta").text_content()
        print(f"meta: {meta!r}")
        if "photo" not in (meta or ""):
            failures.append(f"M0: #meta not populated from /api/status (got {meta!r})")

        foot_version = await page.locator("#footVersion").text_content()
        print(f"footer version: {foot_version!r}")
        if not foot_version or foot_version.strip() in ("", "—"):
            failures.append("M0: #footVersion not populated from /api/config")

        foot_time = await page.locator("#footTime").text_content()
        if not foot_time or foot_time.strip() == "—":
            failures.append("M0: #footTime not populated")

        # ── M2: gallery renders real thumbnails in a justified layout ──────
        pool = await page.evaluate("async () => (await (await fetch('/hokku/api/status')).json()).pool_size")
        await page.wait_for_timeout(400)  # let images decode
        tiles = await page.locator("#gallery .cell-tile").count()
        rows = await page.locator("#gallery .jrow").count()
        empty = await page.locator("#gallery .gempty").count()
        print(f"gallery: pool={pool} tiles={tiles} rows={rows} empty={empty}")
        if pool and tiles == 0:
            failures.append(f"M2: {pool} ready images but 0 tiles rendered")
        if tiles and rows == 0:
            failures.append("M2: tiles present but no justified .jrow wrappers")
        if not pool and empty == 0:
            failures.append("M2: empty pool but no empty-state shown")
        if tiles:
            # every rendered thumbnail actually decoded (naturalWidth > 0)
            broken = await page.evaluate(
                "[...document.querySelectorAll('#gallery .cell-tile img')].filter(i=>i.complete && i.naturalWidth===0).length"
            )
            if broken:
                failures.append(f"M2: {broken} thumbnail(s) failed to load")
            # tiles keep their true aspect ratio (rendered box AR ≈ dataset ar)
            ar_ok = await page.evaluate(
                "[...document.querySelectorAll('#gallery .cell-tile')].every(t=>{"
                "const a=parseFloat(t.dataset.ar),r=t.getBoundingClientRect();"
                "return !r.height||Math.abs((r.width/r.height)-a)<0.06;})"
            )
            if not ar_ok:
                failures.append("M2: a tile's rendered aspect ratio drifted from its image AR")
            # filter switch to Portrait must not crash and must re-render
            await page.locator('#ftabs button[data-filter="portrait"]').click()
            await page.wait_for_timeout(150)
            after = await page.locator("#gallery .cell-tile, #gallery .gempty").count()
            if after == 0:
                failures.append("M2: Portrait filter produced neither tiles nor an empty state")
            # Grouped view builds labelled orientation sections
            await page.locator('#ftabs button[data-filter="grouped"]').click()
            await page.wait_for_timeout(150)
            sections = await page.locator("#gallery .gsection .ghead").count()
            if sections == 0:
                failures.append("M2: Grouped filter produced no labelled sections")
            await page.locator('#ftabs button[data-filter="mixed"]').click()

        # horizontal overflow guard (both form factors)
        overflow = await page.evaluate(
            "document.documentElement.scrollWidth > document.documentElement.clientWidth"
        )
        if overflow:
            failures.append("layout: horizontal overflow")

        # ── M7: resilience + mobile parity ────────────────────────────────
        if await page.locator("#offline-pill").count() == 0:
            failures.append("M7: offline pill markup missing")
        # settings opens; mobile drills down from a category list, desktop shows tabs
        await page.locator("#settings-btn").click()
        await page.wait_for_selector("#settings-modal:not([hidden])", timeout=4000)
        if MOBILE:
            if await page.locator("#set-body .set-list").count() == 0:
                failures.append("M7: mobile settings is not a drill-down list")
            else:
                await page.locator('#set-body [data-li="server"]').click()
                await page.wait_for_timeout(120)
                if await page.locator("#set-back:not([hidden])").count() == 0:
                    failures.append("M7: mobile drill-in has no back chevron")
        elif await page.locator("#set-nav [data-tab]").count() == 0:
            failures.append("M7: desktop settings nav missing")
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(120)
        if await page.locator("#settings-modal[hidden]").count() == 0:
            failures.append("M7: Escape did not close settings")
        # mobile: the wordmark opens the server modal (not the desktop drawer)
        if MOBILE:
            await page.locator("#brand").click()
            await page.wait_for_timeout(150)
            if await page.locator("#server-modal:not([hidden])").count() == 0:
                failures.append("M7: mobile server modal did not open")
            await page.keyboard.press("Escape")

        await browser.close()

    if console_errors:
        failures.append("console errors: " + " | ".join(console_errors[:5]))

    if failures:
        print("\nFAIL")
        for f in failures:
            print("  ✗", f)
        return 1
    print("\nOK — all checks passed")
    return 0


sys.exit(asyncio.run(main()))
