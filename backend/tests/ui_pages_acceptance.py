"""Every tool remains usable in the shared design, in both themes and on mobile.

Uses an isolated SQLite workspace and synthetic model. No user credentials or
external accounts are read. HARNESS_UI_ARTIFACT_DIR optionally saves previews.
"""
from pathlib import Path
import os
import tempfile
import unittest

import httpx
from playwright.sync_api import expect, sync_playwright

from harness.core.config import Config
from tests.ui_acceptance import ROOT, TOKEN, serve


PAGES = ("Goals", "Memory", "History", "People", "Repairs", "Evidence",
         "Schedules", "Tasks", "Workflows", "Browser", "Approvals", "Inbox",
         "Files", "Connections", "Activity")


class PagesUIAcceptance(unittest.TestCase):
    def test_docker_browser_explains_manual_sign_in_availability(self):
        with tempfile.TemporaryDirectory(prefix="harness-browser-control-ui-") as folder:
            root = Path(folder)
            config = Config(root=root, data_dir=root / '.assistant', port=0, token=TOKEN,
                            encryption_secret=TOKEN, container_mode=True,
                            frontend_dir=ROOT / 'frontend/dist')
            with serve(config) as origin, sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    page = browser.new_page()
                    page.goto(origin)
                    page.get_by_label('Workspace access token').fill(TOKEN)
                    page.get_by_role('button', name='Connect workspace', exact=True).click()
                    nav = page.get_by_role('navigation', name='Main navigation')
                    tool = nav.get_by_role('button', name='Browser', exact=True)
                    if not tool.is_visible():
                        nav.get_by_role('button', name='More tools', exact=True).click()
                    tool.click()
                    expect(page.get_by_text('Manual browser sign-in is unavailable in this Docker setup.', exact=False)).to_be_visible()
                    expect(page.get_by_role('button', name='Open browser window', exact=True)).to_have_count(0)
                    expect(page.get_by_role('button', name='Start browser', exact=True)).to_be_visible()
                finally:
                    browser.close()

    def test_every_page_themes_navigation_and_small_screens(self):
        with tempfile.TemporaryDirectory(prefix="harness-pages-ui-") as folder:
            root = Path(folder)
            config = Config(
                root=root, data_dir=root / ".assistant", port=0, token=TOKEN,
                api_key="synthetic-model-no-network", encryption_secret=TOKEN,
                frontend_dir=ROOT / "frontend/dist", browser_channel="chromium",
                run_token_reservation=100, daily_token_limit=100000,
            )
            artifact_dir = Path(os.environ["HARNESS_UI_ARTIFACT_DIR"]).resolve() if os.environ.get("HARNESS_UI_ARTIFACT_DIR") else None
            if artifact_dir:
                artifact_dir.mkdir(parents=True, exist_ok=True)
            with serve(config) as origin, sync_playwright() as playwright:
                with httpx.Client(base_url=origin, trust_env=False, headers={"Authorization": f"Bearer {TOKEN}"}) as api:
                    for path, payload in (
                        ("/api/goals", {"title": "Prepare the project launch", "description": "Review the checklist and share the next steps.", "steps": ["Review the design", "Verify the workflows"]}),
                        ("/api/memories", {"text": "Keep project updates short and useful.", "kind": "preference"}),
                        ("/api/files", {"path": "reports/project-brief.md", "content": "# Project brief\nSynthetic UI preview."}),
                        ("/api/files", {"path": "notes.md", "content": "Synthetic project notes."}),
                        ("/api/actions", {"kind": "skill.save", "summary": "Save the project review procedure", "payload": {"name": "project-review", "content": "# Review the checklist"}}),
                    ):
                        response = api.post(path, json=payload)
                        self.assertLess(response.status_code, 300, response.text)
                browser = playwright.chromium.launch()
                try:
                    page = browser.new_page(viewport={"width": 1440, "height": 1000})
                    page.set_default_timeout(15000)
                    errors, external = [], []
                    page.on("pageerror", lambda error: errors.append(str(error)))

                    def local_only(route):
                        if route.request.url.startswith(origin):
                            route.continue_()
                        else:
                            external.append(route.request.url)
                            route.abort()
                    page.route("**/*", local_only)
                    page.goto(origin)
                    page.get_by_label("Workspace access token", exact=True).fill(TOKEN)
                    page.get_by_role("button", name="Connect workspace", exact=True).click()
                    main_nav = page.get_by_role("navigation", name="Main navigation", exact=True)
                    expect(main_nav).to_be_visible()

                    def navigate(name):
                        target = main_nav.get_by_role("button", name=name, exact=True)
                        if not target.is_visible():
                            main_nav.get_by_role("button", name="More tools", exact=True).click()
                        target.click()
                        expect(page.get_by_role("heading", name=name, exact=True)).to_be_visible()
                        page.wait_for_load_state("networkidle")

                    for theme in ("Dark", "Light"):
                        page.get_by_role("button", name="Settings", exact=True).click()
                        dialog = page.get_by_role("dialog", name="Settings", exact=True)
                        dialog.get_by_role("button", name=theme, exact=True).click()
                        dialog.get_by_role("button", name="Close settings", exact=True).click()
                        for width in (1440, 390):
                            page.set_viewport_size({"width": width, "height": 1000 if width == 1440 else 844})
                            for name in PAGES:
                                with self.subTest(theme=theme, width=width, page=name):
                                    navigate(name)
                                    content = page.locator(".workspace-page-content")
                                    self.assertTrue(content.evaluate("el => el.scrollWidth <= el.clientWidth + 1"), name)
                                    self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), name)
                                    expect(page.get_by_role("alert")).to_have_count(0)
                                    if artifact_dir and name in ("Goals", "Memory", "Files", "Connections", "Approvals"):
                                        page.screenshot(path=str(artifact_dir / f"{name.lower()}-{theme.lower()}-{width}.png"), full_page=True)

                    # Desktop navigation is searchable, with an honest empty state.
                    page.set_viewport_size({"width": 1440, "height": 1000})
                    pages = page.get_by_role("complementary", name="Workspace pages", exact=True)
                    pages.get_by_label("Find a page", exact=True).fill("nothing-matches-this")
                    expect(pages.get_by_text("No pages found. Try a shorter name.", exact=True)).to_be_visible()
                    pages.get_by_label("Find a page", exact=True).fill("files")
                    pages.get_by_role("button", name="Open Files page", exact=True).click()
                    expect(page.get_by_role("heading", name="Files", exact=True)).to_be_visible()
                    expect(page.get_by_role("button", name="Folder reports", exact=False)).to_be_visible()

                    # The mobile drawer must close via keyboard and restore focus.
                    page.set_viewport_size({"width": 390, "height": 844})
                    trigger = page.get_by_role("button", name="Show pages", exact=True)
                    trigger.click()
                    expect(pages.get_by_label("Find a page", exact=True)).to_be_focused()
                    pages.get_by_label("Find a page", exact=True).fill("memory")
                    pages.get_by_role("button", name="Open Memory page", exact=True).click()
                    expect(page.get_by_role("heading", name="Memory", exact=True)).to_be_visible()
                    expect(pages).not_to_be_visible()
                    trigger.click()
                    page.keyboard.press("Escape")
                    expect(pages).not_to_be_visible()
                    expect(trigger).to_be_focused()
                    page.get_by_role("button", name="Back to chat", exact=True).click()
                    expect(page.get_by_role("log", name="Conversation", exact=True)).to_be_visible()
                    self.assertEqual(errors, [])
                    self.assertEqual(external, [])
                finally:
                    browser.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
