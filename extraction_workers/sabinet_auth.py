import asyncio
import os
from playwright.async_api import async_playwright

async def generate_state():
    print("==================================================")
    print(" 🌌 COEUS SABINET SESSION STATE GENERATOR")
    print("==================================================")
    
    # Target file paths
    save_path_root = os.path.join("data", "state.json")
    save_path_ccma = os.path.join("data", "sabinet_ccma", "html", "state.json")
    
    os.makedirs(os.path.join("data", "sabinet_ccma", "html"), exist_ok=True)
    
    async with async_playwright() as p:
        print("\n[INFO] Launching headed Chromium browser...")
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        print("💡 Navigating to Sabinet CCMA Awards page...")
        await page.goto("https://discover.sabinet.co.za/search?Search=&ProductType=ccmabargainingcouncilawards")
        
        print("\n=================== ACTION REQUIRED ===================")
        print("1. Click the 'Sign in' link at the top-right of the page.")
        print("2. Choose 'Sign in with Google' and complete the auth flow.")
        print("3. Once logged in and redirected back, verify you can see the content.")
        print("=======================================================")
        
        input("\n👉 Press ENTER here in the terminal once you've successfully logged in to save cookies...")
        
        # Save state to both the root and pipeline-specific locations
        await context.storage_state(path=save_path_root)
        await context.storage_state(path=save_path_ccma)
        
        print(f"\n✅ Success! Session state saved to:")
        print(f"   - {save_path_root}")
        print(f"   - {save_path_ccma}")
        
        await browser.close()
        print("Browser closed.")

if __name__ == "__main__":
    asyncio.run(generate_state())
