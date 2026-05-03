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
import sys
import subprocess

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

async def generate_voiceover(text, job_id, character='spongebob'):
    """Async function to generate voiceover for specified character"""
    global active_browsers
    
    async with browser_semaphore:
        with browser_lock:
            active_browsers += 1
        
        try:
            # Get the URL for the selected character
            character = character.lower().replace(' ', '')
            if character not in CHARACTER_URLS:
                character = 'spongebob'
            
            voice_url = CHARACTER_URLS[character]
            
            async with async_playwright() as p:
                browser = await p.chromium.launch(
                    headless=True,
                    args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
                )
                context = await browser.new_context()
                page = await context.new_page()
                
                # Update job status
                with job_lock:
                    jobs[job_id]['status'] = 'processing'
                    jobs[job_id]['character'] = character
                
                # Navigate to character page
                await page.goto(voice_url, wait_until="networkidle")
                await asyncio.sleep(2)
                
                # Type text in textarea
                textarea = await page.query_selector('textarea.textarea')
                if textarea:
                    await textarea.fill(text)
                
                # Click generate button
                generate_button = await page.query_selector('button.btn-primary:has-text("Generate Voiceover")')
                if generate_button:
                    await generate_button.click()
                
                # Wait for audio URL
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
    """Generate voiceover and wait for completion (synchronous)"""
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
        return jsonify({
            'success': False,
            'error': job.get('error', 'Generation failed')
        }), 500

@app.route('/generate', methods=['GET'])
def generate():
    """Start async voice generation"""
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
    
    thread = threading.Thread(target=run_async_task, args=(text, job_id, character))
    thread.daemon = True
    thread.start()
    
    return jsonify({
        'success': True,
        'job_id': job_id,
        'character': character,
        'message': f'{character.capitalize()} voice generation started',
        'status_url': f'/status?job_id={job_id}'
    })

@app.route('/batch-500', methods=['POST'])
def batch_500():
    """Generate 500 voices concurrently"""
    data = request.json
    
    if not data or 'texts' not in data:
        return jsonify({
            'success': False,
            'error': 'Please provide 500 texts in the "texts" array'
        }), 400
    
    texts = data['texts']
    character = data.get('character', 'spongebob').strip()
    
    if len(texts) != 500:
        return jsonify({
            'success': False,
            'error': f'Please provide exactly 500 texts, got {len(texts)}'
        }), 400
    
    job_ids = []
    
    with job_lock:
        for text in texts:
            job_id = str(uuid.uuid4())
            job_ids.append(job_id)
            jobs[job_id] = {
                'status': 'pending',
                'text': text,
                'character': character,
                'created_at': datetime.now().isoformat()
            }
    
    # Start all 500 generations
    for job_id, text in zip(job_ids, texts):
        thread = threading.Thread(target=run_async_task, args=(text, job_id, character))
        thread.daemon = True
        thread.start()
    
    return jsonify({
        'success': True,
        'message': f'Started 500 voice generations with character {character}',
        'max_concurrent': 500,
        'job_ids': job_ids,
        'status_endpoint': '/batch-status'
    })

@app.route('/batch-status', methods=['POST'])
def batch_status():
    """Get status for multiple jobs"""
    data = request.json
    
    if not data or 'job_ids' not in data:
        return jsonify({
            'success': False,
            'error': 'Please provide job_ids array'
        }), 400
    
    job_ids = data['job_ids']
    results = {}
    
    with job_lock:
        for job_id in job_ids:
            if job_id in jobs:
                job = jobs[job_id].copy()
                results[job_id] = {
                    'status': job['status'],
                    'text': job['text'],
                    'character': job['character']
                }
                if job['status'] == 'completed':
                    results[job_id]['audio_url'] = job['audio_url']
                elif job['status'] == 'failed':
                    results[job_id]['error'] = job.get('error')
    
    return jsonify({
        'success': True,
        'results': results
    })

@app.route('/stats', methods=['GET'])
def stats():
    """Get system statistics"""
    with job_lock:
        total = len(jobs)
        completed = len([j for j in jobs.values() if j['status'] == 'completed'])
        failed = len([j for j in jobs.values() if j['status'] == 'failed'])
        processing = len([j for j in jobs.values() if j['status'] == 'processing'])
        pending = len([j for j in jobs.values() if j['status'] == 'pending'])
    
    with browser_lock:
        current = active_browsers
    
    return jsonify({
        'success': True,
        'jobs': {
            'total': total,
            'completed': completed,
            'failed': failed,
            'processing': processing,
            'pending': pending
        },
        'concurrent': {
            'max': 500,
            'active': current,
            'available': 500 - current
        }
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

@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({'success': True, 'message': 'pong'})

@app.route('/health', methods=['GET'])
def health():
    with job_lock:
        active_jobs = len([j for j in jobs.values() if j['status'] == 'processing'])
    
    return jsonify({
        'status': 'healthy',
        'active_jobs': active_jobs,
        'max_concurrent': 500,
        'active_browsers': active_browsers
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    print("=" * 60)
    print("🚀 500 Concurrent Voice Generation Server")
    print("=" * 60)
    print(f"📍 Port: {port}")
    print(f"⚡ Max concurrent: 500 browsers")
    print(f"🏓 Ping: /ping")
    print(f"🎤 Generate: /generate-and-wait?text=hello&character=spongebob")
    print(f"📦 Batch 500: POST /batch-500 with 500 texts")
    print("=" * 60)
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
