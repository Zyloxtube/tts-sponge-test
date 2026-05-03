from flask import Flask, request, jsonify
from flask_cors import CORS
import asyncio
import uuid
from playwright.async_api import async_playwright
import nest_asyncio
from datetime import datetime
import os
import threading

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

# Set browser path for Render
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = '/opt/render/.cache/ms-playwright'

# Semaphore for concurrent browsers (Render free tier)
browser_semaphore = asyncio.Semaphore(3)
active_browsers = 0
browser_lock = threading.Lock()

async def generate_single_voiceover(text, job_id, character='spongebob'):
    """Generate a single voiceover"""
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
                # Launch with Render-compatible settings
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        '--no-sandbox',
                        '--disable-setuid-sandbox',
                        '--disable-dev-shm-usage',
                        '--disable-gpu'
                    ]
                )
                
                context = await browser.new_context()
                page = await context.new_page()
                
                with job_lock:
                    jobs[job_id]['status'] = 'processing'
                
                try:
                    await page.goto(voice_url, wait_until="networkidle", timeout=30000)
                    
                    # Type text
                    textarea = await page.query_selector('textarea.textarea')
                    if textarea:
                        await textarea.fill(text)
                    
                    # Click generate
                    generate_button = await page.query_selector('button.btn-primary:has-text("Generate Voiceover")')
                    if generate_button:
                        await generate_button.click()
                    
                    # Wait for audio
                    audio_url = None
                    for _ in range(60):  # 30 seconds max
                        audio_element = await page.query_selector('audio[src*=".mp3"]')
                        if audio_element:
                            audio_url = await audio_element.get_attribute('src')
                            if audio_url:
                                break
                        await asyncio.sleep(0.5)
                    
                    if audio_url:
                        with job_lock:
                            jobs[job_id]['status'] = 'completed'
                            jobs[job_id]['audio_url'] = audio_url
                    else:
                        raise Exception("Audio generation timeout")
                        
                finally:
                    await browser.close()
                    
        except Exception as e:
            with job_lock:
                jobs[job_id]['status'] = 'failed'
                jobs[job_id]['error'] = str(e)
        
        finally:
            with browser_lock:
                active_browsers -= 1

@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({'success': True, 'message': 'pong', 'timestamp': datetime.now().isoformat()})

@app.route('/ping-simple', methods=['GET'])
def ping_simple():
    return 'pong', 200, {'Content-Type': 'text/plain'}

@app.route('/generate', methods=['GET'])
def generate():
    text = request.args.get('text', '').strip()
    character = request.args.get('character', 'spongebob').strip()
    
    if not text:
        return jsonify({'success': False, 'error': 'No text provided'}), 400
    
    if character.lower() not in CHARACTER_URLS:
        return jsonify({'success': False, 'error': f'Invalid character'}), 400
    
    job_id = str(uuid.uuid4())
    
    with job_lock:
        jobs[job_id] = {
            'status': 'pending',
            'text': text,
            'character': character,
            'created_at': datetime.now().isoformat()
        }
    
    def run_async():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(generate_single_voiceover(text, job_id, character))
        loop.close()
    
    thread = threading.Thread(target=run_async)
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
        return jsonify({
            'success': False,
            'status': 'failed',
            'error': job.get('error', 'Unknown error')
        }), 500
    else:
        return jsonify({
            'success': True,
            'status': job['status'],
            'message': 'Processing...'
        })

@app.route('/characters', methods=['GET'])
def get_characters():
    return jsonify({
        'success': True,
        'characters': list(CHARACTER_URLS.keys())
    })

@app.route('/health', methods=['GET'])
def health():
    with job_lock:
        active = len([j for j in jobs.values() if j['status'] == 'processing'])
    return jsonify({
        'status': 'healthy',
        'active_jobs': active,
        'max_concurrent': 3
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    print(f"Starting server on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)
