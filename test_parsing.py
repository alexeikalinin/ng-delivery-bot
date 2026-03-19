"""
Ищем селектор подзаголовка (MINI) в карточках sadypobedy.by
"""
import asyncio
from playwright.async_api import async_playwright


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto("https://sadypobedy.by/fastfud", timeout=30000)
        await page.wait_for_timeout(5000)

        cards = await page.query_selector_all(".t-store__card")
        # Смотрим вторую карточку — это «Шаурма царская MINI»
        card = cards[1]
        html = await card.inner_html()
        print("HTML второй карточки (MINI):")
        print(html[:3000])

if __name__ == "__main__":
    asyncio.run(main())
