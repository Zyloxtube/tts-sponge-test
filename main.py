# main.py
from flask import Flask, request, jsonify
from flask_cors import CORS
import asyncio
import threading
import uuid
from playwright.async_api import async_playwright
import nest_asyncio
from datetime import datetime
import os
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

# Semaphore for 500 concurrent browsers
browser_semaphore = asyncio.Semaphore(500)

active_browsers = 0
browser_lock = threading.Lock()

def find_chrome_path():
    """Find the actual Chrome executable path"""
    # Look for any Chrome/Chromium executable in the cache
    paths = [
        '/opt/render/.cache/ms-playwright/chromium-*/chrome-linux/chrome',
        '/opt/render/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell',
        '/opt/render/.cache/ms-playwright/*/chrome-linux/chrome',
        '/opt/render/.cache/ms-playwright/*/chrome',
    ]
    
    for pattern in paths:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    return None

# Find Chrome at startup
CHROME_PATH = find_chrome_path()
print(f"Chrome path found: {CHROME_PATH}")

async def generate_voiceover(text, job_id, character='spongebob'):
    """Async function to generate voiceover for specified character"""
    global active_browsers
    
    async with browser_semaphore:
        with browser_lock:
            active_browsers += 1
        
        try:
            character = character.lower().replace(' ', '')
            if character not in CHARACTER_URLS:
                character = 'spongebob'
            
            voice_url = CHARACTER_URLS[character]
            
            async with async_playwright() as p:
                # Launch with found Chrome path or let it auto-detect
                if CHROME_PATH:
                    browser = await p.chromium.launch(
                        headless=True,
                        executable_path=CHROME_PATH,
                        args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
                    )
                else:
                    browser = await p.chromium.launch(
                        headless=True,
                        args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
                    )
                
                context = await browser.new_context()
                page = await context.new_page()
                
                with job_lock:
                    jobs[job_id]['status'] = 'processing'
                    jobs[job_id]['character'] = character
                
                await page.goto(voice_url, wait_until="networkidle")
                await asyncio.sleep(2)
                
                textarea = await page.query_selector('textarea.textarea')
                if textarea:
                    await textarea.fill(text)
                
                generate_button = await page.query_selector('button.btn-primary:has-text("Generate Voiceover")')
                if generate_button:
                    await generate_button.click()
                
                audio_url = None
                attempts = 0
                max_attempts = 180
                
                while attempts < max_attempts:
                    audio_element = await page.query_selector('audio[src*=".mp3"]')
                    if audio_element:
                        audio_url = await audio_element.get_attribute('src')
                        if audio_url:
                            break
                    await asyncio.sleep(0.5)
                    attempts += 1
                
                await browser.close()
                
                if audio_url:
                    with job_lock:
                        jobs[job_id]['status'] = 'completed'
                        jobs[job_id]['audio_url'] = audio_url
                        jobs[job_id]['completed_at'] = datetime.now().isoformat()
                else:
                    with job_lock:
                        jobs[job_id]['status'] = 'failed'
                        jobs[job_id]['error'] = 'Timeout: Audio generation took too long'
                
        except Exception as e:
            with job_lock:
                jobs[job_id]['status'] = 'failed'
                jobs[job_id]['error'] = str(e)
        
        finally:
            with browser_lock:
                active_browsers -= 1

def run_async_task(text, job_id, character):
    """Run async task in new event loop"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(generate_voiceover(text, job_id, character))
    loop.close()

@app.route('/generate-and-wait', methods=['GET'])
def generate_and_wait():
    """Generate voiceover and wait for completion"""
    text = request.args.get('text', '').strip()
    character = request.args.get('character', 'spongebob').strip()
    
    if not text:
        return jsonify({'success': False, 'error': 'No text provided'}), 400
    
    if character.lower() not in CHARACTER_URLS:
        return jsonify({'success': False, 'error': 'Invalid character'}), 400
    
    job_id = str(uuid.uuid4())
    with job_lock:
        jobs[job_id] = {
            'status': 'pending',
            'text': text,
            'character': character,
            'created_at': datetime.now().isoformat()
        }
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(generate_voiceover(text, job_id, character))
    loop.close()
    
    with job_lock:
        job = jobs[job_id].copy()
    
    if job['status'] == 'completed':
        return jsonify({
            'success': True,
            'audio_url': job['audio_url'],
            'text': text,
            'character': character
        })
    else:
        return jsonify({'success': False, 'error': job.get('error', 'Generation failed')}), 500

@app.route('/generate', methods=['GET'])
def generate():
    """Start async voice generation"""
    text = request.args.get('text', '').strip()
    character = request.args.get('character', 'spongebob').strip()
    
    if not text:
        return jsonify({'success': False, 'error': 'No text provided'}), 400
    
    if character.lower() not in CHARACTER_URLS:
        return jsonify({'success': False, 'error': 'Invalid character'}), 400
    
    job_id = str(uuid.uuid4())
    with job_lock:
        jobs[job_id] = {
            'status': 'pending',
            'text': text,
            'character': character,
            'created_at': datetime.now().isoformat()
        }
    
    thread = threading.Thread(target=run_async_task, args=(text, job_id, character))
    thread.daemon = True
    thread.start()
    
    return jsonify({
        'success': True,
        'job_id': job_id,
        'character': character,
        'status_url': f'/status?job_id={job_id}'
    })

@app.route('/status', methods=['GET'])
def get_status():
    """Get job status"""
    job_id = request.args.get('job_id', '')
    
    with job_lock:
        if job_id not in jobs:
            return jsonify({'success': False, 'error': 'Invalid job_id'}), 404
        job = jobs[job_id].copy()
    
    if job['status'] == 'completed':
        return jsonify({
            'success': True,
            'status': 'completed',
            'audio_url': job['audio_url'],
            'text': job['text'],
            'character': job['character']
        })
    elif job['status'] == 'failed':
        return jsonify({'success': False, 'error': job.get('error')}), 500
    else:
        return jsonify({'success': True, 'status': job['status']})

@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({'success': True, 'message': 'pong'})

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'healthy',
        'chrome_path': CHROME_PATH,
        'active_browsers': active_browsers,
        'max_concurrent': 500
    })

@app.route('/characters', methods=['GET'])
def get_characters():
    return jsonify({'characters': list(CHARACTER_URLS.keys())})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    print(f"Chrome found at: {CHROME_PATH}")
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
