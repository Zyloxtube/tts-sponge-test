from flask import Flask, request, jsonify
from flask_cors import CORS
import asyncio
import uuid
from playwright.async_api import async_playwright
import nest_asyncio
from datetime import datetime
import os
import threading
import glob

app = Flask(__name__)
CORS(app)

nest_asyncio.apply()

# Character URL mapping
CHARACTER_URLS = {
    'spongebob': 'https://nicevoice.org/ai-voice-generator/spongebob-squarepants/',
    'patrick': 'https://nicevoice.org/ai-voice-generator/patrick-star/',
    'squidward': 'https://nicevoice.org/ai-voice-generator/squidward-tentacles/',
    'mrkrabs': 'https://nicevoice.org/ai-voice-generator/mr-krabs/'
}

# Store job status
jobs = {}
job_lock = threading.Lock()

# Semaphore for concurrent browsers
browser_semaphore = asyncio.Semaphore(2)

def find_browser_executable():
    """Find the actual Chrome executable path"""
    possible_paths = [
        "/opt/render/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
        "/opt/render/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
        "/opt/render/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
    ]
    
    for pattern in possible_paths:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    return None

async def generate_voiceover_direct(text, character='spongebob'):
    """Generate voiceover and return audio URL directly"""
    try:
        character = character.lower().replace(' ', '')
        if character not in CHARACTER_URLS:
            character = 'spongebob'
        
        voice_url = CHARACTER_URLS[character]
        
        async with browser_semaphore:
            async with async_playwright() as p:
                # Find the browser executable
                browser_exe = find_browser_executable()
                
                if browser_exe:
                    print(f"Found browser at: {browser_exe}")
                    browser = await p.chromium.launch(
                        headless=True,
                        executable_path=browser_exe,
                        args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
                    )
                else:
                    print("No browser found, letting Playwright find it")
                    browser = await p.chromium.launch(
                        headless=True,
                        args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
                    )
                
                context = await browser.new_context()
                page = await context.new_page()
                
                try:
                    # Navigate to character page
                    await page.goto(voice_url, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(2)
                    
                    # Type text
                    textarea = await page.query_selector('textarea.textarea')
                    if textarea:
                        await textarea.fill(text)
                    else:
                        raise Exception("Textarea not found on page")
                    
                    # Click generate button
                    generate_button = await page.query_selector('button.btn-primary:has-text("Generate Voiceover")')
                    if generate_button:
                        await generate_button.click()
                    else:
                        raise Exception("Generate button not found")
                    
                    # Wait for audio URL
                    audio_url = None
                    for attempt in range(60):
                        try:
                            audio_element = await page.query_selector('audio[src*=".mp3"]')
                            if audio_element:
                                audio_url = await audio_element.get_attribute('src')
                                if audio_url and audio_url.startswith('http'):
                                    break
                        except:
                            pass
                        await asyncio.sleep(0.5)
                    
                    return audio_url
                    
                finally:
                    await browser.close()
                    
    except Exception as e:
        raise Exception(f"Generation failed: {str(e)}")

@app.route('/generate-and-wait', methods=['GET'])
def generate_and_wait():
    """Generate voice and wait for completion"""
    text = request.args.get('text', '').strip()
    character = request.args.get('character', 'spongebob').strip()
    
    if not text:
        return jsonify({
            'success': False,
            'error': 'No text provided. Use ?text=your_text_here'
        }), 400
    
    if character.lower() not in CHARACTER_URLS:
        return jsonify({
            'success': False,
            'error': f'Invalid character. Use: {", ".join(CHARACTER_URLS.keys())}'
        }), 400
    
    try:
        # Run async generation
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        audio_url = loop.run_until_complete(generate_voiceover_direct(text, character))
        loop.close()
        
        if audio_url:
            return jsonify({
                'success': True,
                'audio_url': audio_url,
                'text': text,
                'character': character,
                'message': 'Voice generation completed'
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Timeout - no audio generated'
            }), 500
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({'success': True, 'message': 'pong', 'timestamp': datetime.now().isoformat()})

@app.route('/ping-simple', methods=['GET'])
def ping_simple():
    return 'pong', 200, {'Content-Type': 'text/plain'}

@app.route('/characters', methods=['GET'])
def get_characters():
    return jsonify({
        'success': True,
        'characters': list(CHARACTER_URLS.keys())
    })

@app.route('/health', methods=['GET'])
def health():
    browser_path = find_browser_executable()
    return jsonify({
        'status': 'healthy',
        'browser_found': browser_path is not None,
        'browser_path': browser_path
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    
    print("=" * 60)
    print("🚀 Voice Generation Server Starting")
    print("=" * 60)
    print(f"📍 Port: {port}")
    
    # Check for browser
    browser_path = find_browser_executable()
    if browser_path:
        print(f"✅ Browser found at: {browser_path}")
    else:
        print(f"⚠️  Browser not found - will attempt auto-detection")
        print(f"   Cache dir: /opt/render/.cache/ms-playwright/")
    
    print(f"🏓 Ping: /ping")
    print(f"🎤 Generate: /generate-and-wait?text=hello&character=spongebob")
    print("=" * 60)
    
    app.run(host='0.0.0.0', port=port, debug=False)
