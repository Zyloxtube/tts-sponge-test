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

# Store job status (for background jobs)
jobs = {}
job_lock = threading.Lock()

# Semaphore for concurrent browsers
browser_semaphore = asyncio.Semaphore(3)

async def generate_voiceover_direct(text, character='spongebob'):
    """Generate voiceover and return audio URL directly (no job storage)"""
    try:
        character = character.lower().replace(' ', '')
        if character not in CHARACTER_URLS:
            character = 'spongebob'
        
        voice_url = CHARACTER_URLS[character]
        
        async with browser_semaphore:
            async with async_playwright() as p:
                # Launch browser
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
                
                try:
                    # Navigate to character page
                    await page.goto(voice_url, wait_until="networkidle", timeout=30000)
                    
                    # Type text
                    textarea = await page.query_selector('textarea.textarea')
                    if textarea:
                        await textarea.fill(text)
                    
                    # Click generate button
                    generate_button = await page.query_selector('button.btn-primary:has-text("Generate Voiceover")')
                    if generate_button:
                        await generate_button.click()
                    
                    # Wait for audio URL
                    audio_url = None
                    for _ in range(60):  # 30 seconds max
                        audio_element = await page.query_selector('audio[src*=".mp3"]')
                        if audio_element:
                            audio_url = await audio_element.get_attribute('src')
                            if audio_url:
                                break
                        await asyncio.sleep(0.5)
                    
                    return audio_url
                    
                finally:
                    await browser.close()
                    
    except Exception as e:
        raise Exception(f"Generation failed: {str(e)}")

@app.route('/generate-and-wait', methods=['GET'])
def generate_and_wait():
    """Generate voice and wait for completion - returns audio URL directly"""
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
    
    try:
        # Run the async generation synchronously
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
                'message': 'Voice generation completed successfully'
            })
        else:
            return jsonify({
                'success': False,
                'error': 'Failed to generate audio: Timeout'
            }), 500
            
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

# Keep the original async endpoints for batch processing
async def generate_voiceover_async(text, job_id, character='spongebob'):
    """Async version for background jobs"""
    try:
        character = character.lower().replace(' ', '')
        if character not in CHARACTER_URLS:
            character = 'spongebob'
        
        voice_url = CHARACTER_URLS[character]
        
        async with browser_semaphore:
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
                )
                
                context = await browser.new_context()
                page = await context.new_page()
                
                with job_lock:
                    jobs[job_id]['status'] = 'processing'
                
                await page.goto(voice_url, wait_until="networkidle", timeout=30000)
                
                textarea = await page.query_selector('textarea.textarea')
                if textarea:
                    await textarea.fill(text)
                
                generate_button = await page.query_selector('button.btn-primary:has-text("Generate Voiceover")')
                if generate_button:
                    await generate_button.click()
                
                audio_url = None
                for _ in range(60):
                    audio_element = await page.query_selector('audio[src*=".mp3"]')
                    if audio_element:
                        audio_url = await audio_element.get_attribute('src')
                        if audio_url:
                            break
                    await asyncio.sleep(0.5)
                
                await browser.close()
                
                if audio_url:
                    with job_lock:
                        jobs[job_id]['status'] = 'completed'
                        jobs[job_id]['audio_url'] = audio_url
                        jobs[job_id]['completed_at'] = datetime.now().isoformat()
                else:
                    with job_lock:
                        jobs[job_id]['status'] = 'failed'
                        jobs[job_id]['error'] = 'Timeout'
                    
    except Exception as e:
        with job_lock:
            jobs[job_id]['status'] = 'failed'
            jobs[job_id]['error'] = str(e)

@app.route('/generate', methods=['GET'])
def generate():
    """Start async voice generation (non-blocking)"""
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
        loop.run_until_complete(generate_voiceover_async(text, job_id, character))
        loop.close()
    
    thread = threading.Thread(target=run_async)
    thread.daemon = True
    thread.start()
    
    return jsonify({
        'success': True,
        'job_id': job_id,
        'character': character,
        'message': 'Voice generation started',
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
            'character': job['character'],
            'created_at': job['created_at'],
            'completed_at': job.get('completed_at')
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
            'message': 'Still processing...'
        })

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
        'characters': list(CHARACTER_URLS.keys()),
        'default': 'spongebob'
    })

@app.route('/health', methods=['GET'])
def health():
    with job_lock:
        active_jobs = len([j for j in jobs.values() if j['status'] == 'processing'])
    
    return jsonify({
        'status': 'healthy',
        'active_jobs': active_jobs,
        'max_concurrent': 3,
        'total_jobs': len(jobs)
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    print("=" * 60)
    print("🚀 Voice Generation Server")
    print("=" * 60)
    print(f"📍 Running on port: {port}")
    print(f"⚡ Generate & wait: /generate-and-wait?text=hello&character=spongebob")
    print(f"🔄 Async generate: /generate?text=hello&character=spongebob")
    print(f"🏓 Health check: /ping")
    print("=" * 60)
    
    app.run(host='0.0.0.0', port=port, debug=False)
