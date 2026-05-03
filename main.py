# main.py
from flask import Flask, request, jsonify
from flask_cors import CORS
import asyncio
import uuid
from playwright.async_api import async_playwright
import nest_asyncio
from datetime import datetime
import os
import threading
import gc

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

# Semaphore for concurrent browsers (Render has limited resources)
# Start with lower concurrency for Render's free tier
browser_semaphore = asyncio.Semaphore(5)  # Reduced to 5 for Render

# Track active browser count
active_browsers = 0
browser_lock = threading.Lock()

async def generate_single_voiceover(text, job_id, character='spongebob'):
    """Generate a single voiceover with semaphore control"""
    global active_browsers
    
    async with browser_semaphore:
        with browser_lock:
            active_browsers += 1
            current_active = active_browsers
            
        print(f"Job {job_id[:8]} started. Active browsers: {current_active}/5")
        
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
                        '--disable-gpu',
                        '--single-process'  # Helps with memory on Render
                    ]
                )
                
                context = await browser.new_context(
                    viewport={'width': 400, 'height': 300}
                )
                
                page = await context.new_page()
                
                with job_lock:
                    jobs[job_id]['status'] = 'processing'
                    jobs[job_id]['character'] = character
                
                await page.goto(voice_url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(1)
                
                textarea = await page.query_selector('textarea.textarea')
                if textarea:
                    await textarea.fill(text)
                
                generate_button = await page.query_selector('button.btn-primary:has-text("Generate Voiceover")')
                if generate_button:
                    await generate_button.click()
                
                audio_url = None
                attempts = 0
                max_attempts = 120
                
                while attempts < max_attempts:
                    audio_element = await page.query_selector('audio[src*=".mp3"]')
                    if audio_element:
                        audio_url = await audio_element.get_attribute('src')
                        if audio_url and audio_url.startswith('http'):
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

@app.route('/ping', methods=['GET'])
def ping():
    """Simple ping endpoint to check if server is alive"""
    return jsonify({
        'success': True,
        'message': 'pong',
        'timestamp': datetime.now().isoformat()
    })

@app.route('/ping-simple', methods=['GET'])
def ping_simple():
    """Super simple ping endpoint that returns plain text"""
    return 'pong', 200, {'Content-Type': 'text/plain'}

@app.route('/generate', methods=['GET'])
def generate():
    """Start async voice generation for single sound"""
    text = request.args.get('text', '').strip()
    character = request.args.get('character', 'spongebob').strip()
    
    if not text:
        return jsonify({
            'success': False,
            'error': 'No text provided. Please add ?text=your_text_here'
        }), 400
    
    if character.lower() not in CHARACTER_URLS:
        return jsonify({
            'success': False,
            'error': f'Invalid character. Choose from: {", ".join(CHARACTER_URLS.keys())}'
        }), 400
    
    job_id = str(uuid.uuid4())
    
    with job_lock:
        jobs[job_id] = {
            'status': 'pending',
            'text': text,
            'character': character,
            'created_at': datetime.now().isoformat()
        }
    
    def run_async_single():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(generate_single_voiceover(text, job_id, character))
        loop.close()
    
    thread = threading.Thread(target=run_async_single)
    thread.daemon = True
    thread.start()
    
    return jsonify({
        'success': True,
        'job_id': job_id,
        'character': character,
        'message': f'{character.capitalize()} voice generation started',
        'status_url': f'/status?job_id={job_id}'
    })

@app.route('/status', methods=['GET'])
def get_status():
    """Get job status and audio URL when ready"""
    job_id = request.args.get('job_id', '')
    
    with job_lock:
        if not job_id or job_id not in jobs:
            return jsonify({
                'success': False,
                'error': 'Invalid or missing job_id'
            }), 404
        
        job = jobs[job_id].copy()
    
    if job['status'] == 'completed':
        return jsonify({
            'success': True,
            'status': 'completed',
            'audio_url': job['audio_url'],
            'text': job['text'],
            'character': job['character'],
            'created_at': job['created_at'],
            'completed_at': job.get('completed_at')
        })
    elif job['status'] == 'failed':
        return jsonify({
            'success': False,
            'status': 'failed',
            'error': job.get('error', 'Unknown error'),
            'text': job['text'],
            'character': job['character']
        }), 500
    else:
        return jsonify({
            'success': True,
            'status': job['status'],
            'message': 'Still processing... check back soon',
            'text': job['text'],
            'character': job['character']
        })

@app.route('/characters', methods=['GET'])
def get_characters():
    """Get list of available characters"""
    return jsonify({
        'success': True,
        'characters': list(CHARACTER_URLS.keys()),
        'default': 'spongebob'
    })

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    with job_lock:
        active_jobs = len([j for j in jobs.values() if j['status'] == 'processing'])
    
    return jsonify({
        'status': 'healthy',
        'active_jobs': active_jobs,
        'max_concurrent': 5,
        'total_jobs_processed': len(jobs)
    })

if __name__ == '__main__':
    # Get port from environment variable (Render sets this)
    port = int(os.environ.get('PORT', 10000))
    
    print("=" * 60)
    print("🚀 Voice Generation Server Starting on Render")
    print("=" * 60)
    print(f"📊 Max Concurrent Jobs: 5 (Render optimized)")
    print(f"🏓 Ping endpoint: /ping")
    print(f"🌐 Server will bind to 0.0.0.0:{port}")
    print("=" * 60)
    
    # Bind to 0.0.0.0 and Render's PORT
    app.run(host='0.0.0.0', port=port, debug=False)
