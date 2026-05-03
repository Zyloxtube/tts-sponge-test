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
from concurrent.futures import ThreadPoolExecutor

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

# Concurrency control - allows up to 500 simultaneous processes
MAX_CONCURRENT_PROCESSES = 500
semaphore = threading.Semaphore(MAX_CONCURRENT_PROCESSES)
thread_pool = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_PROCESSES)

async def generate_voiceover(text, job_id, character='spongebob'):
    """Async function to generate voiceover for specified character"""
    try:
        # Get the URL for the selected character
        character = character.lower().replace(' ', '')
        if character not in CHARACTER_URLS:
            character = 'spongebob'  # Default to SpongeBob if invalid
        
        voice_url = CHARACTER_URLS[character]
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
            )
            context = await browser.new_context()
            page = await context.new_page()
            
            # Update job status
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
            
            # Wait for audio URL - checking every 0.5 seconds
            audio_url = None
            attempts = 0
            max_attempts = 180  # 90 seconds total
            
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
                jobs[job_id]['status'] = 'completed'
                jobs[job_id]['audio_url'] = audio_url
                jobs[job_id]['completed_at'] = datetime.now().isoformat()
            else:
                jobs[job_id]['status'] = 'failed'
                jobs[job_id]['error'] = 'Timeout: Audio generation took too long'
                
    except Exception as e:
        jobs[job_id]['status'] = 'failed'
        jobs[job_id]['error'] = str(e)

def run_async_task(text, job_id, character):
    """Run async task in new event loop with semaphore control"""
    # Acquire semaphore to limit concurrent processes
    semaphore.acquire()
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(generate_voiceover(text, job_id, character))
        loop.close()
    finally:
        semaphore.release()

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
    
    # Validate character
    if character.lower() not in CHARACTER_URLS:
        return jsonify({
            'success': False,
            'error': f'Invalid character. Choose from: {", ".join(CHARACTER_URLS.keys())}'
        }), 400
    
    # Check if we're at capacity
    if len([j for j in jobs.values() if j['status'] == 'processing']) >= MAX_CONCURRENT_PROCESSES:
        return jsonify({
            'success': False,
            'error': f'Server at maximum capacity ({MAX_CONCURRENT_PROCESSES} simultaneous processes). Please try again later.'
        }), 503
    
    # Run synchronously (will block until complete)
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        'status': 'pending',
        'text': text,
        'character': character,
        'created_at': datetime.now().isoformat()
    }
    
    # Run the async function synchronously with semaphore
    semaphore.acquire()
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(generate_voiceover(text, job_id, character))
        loop.close()
    finally:
        semaphore.release()
    
    job = jobs[job_id]
    
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
    
    # Validate character
    if character.lower() not in CHARACTER_URLS:
        return jsonify({
            'success': False,
            'error': f'Invalid character. Choose from: {", ".join(CHARACTER_URLS.keys())}'
        }), 400
    
    # Check if we're at capacity
    active_jobs = len([j for j in jobs.values() if j['status'] == 'processing'])
    if active_jobs >= MAX_CONCURRENT_PROCESSES:
        return jsonify({
            'success': False,
            'error': f'Server at maximum capacity ({MAX_CONCURRENT_PROCESSES} simultaneous processes). Please try again later.',
            'active_jobs': active_jobs,
            'max_capacity': MAX_CONCURRENT_PROCESSES
        }), 503
    
    # Create job
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        'status': 'pending',
        'text': text,
        'character': character,
        'created_at': datetime.now().isoformat()
    }
    
    # Start generation in background thread using thread pool
    future = thread_pool.submit(run_async_task, text, job_id, character)
    
    return jsonify({
        'success': True,
        'job_id': job_id,
        'character': character,
        'message': f'{character.capitalize()} voice generation started',
        'status_url': f'/status?job_id={job_id}',
        'active_jobs': active_jobs + 1,
        'max_capacity': MAX_CONCURRENT_PROCESSES
    })

@app.route('/status', methods=['GET'])
def get_status():
    """Get job status and audio URL when ready"""
    job_id = request.args.get('job_id', '')
    
    if not job_id or job_id not in jobs:
        return jsonify({
            'success': False,
            'error': 'Invalid or missing job_id'
        }), 404
    
    job = jobs[job_id]
    
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
    active_jobs = len([j for j in jobs.values() if j['status'] == 'processing'])
    pending_jobs = len([j for j in jobs.values() if j['status'] == 'pending'])
    return jsonify({
        'status': 'healthy',
        'active_jobs': active_jobs,
        'pending_jobs': pending_jobs,
        'max_concurrent': MAX_CONCURRENT_PROCESSES,
        'available_slots': MAX_CONCURRENT_PROCESSES - active_jobs
    })

@app.route('/capacity', methods=['GET'])
def get_capacity():
    """Get current server capacity usage"""
    active_jobs = len([j for j in jobs.values() if j['status'] == 'processing'])
    return jsonify({
        'success': True,
        'max_capacity': MAX_CONCURRENT_PROCESSES,
        'active_processes': active_jobs,
        'available_slots': MAX_CONCURRENT_PROCESSES - active_jobs,
        'total_jobs': len(jobs)
    })

if __name__ == '__main__':
    # Install playwright browsers if needed
    os.system('playwright install chromium')
    # Increase thread pool size for better concurrency
    import sys
    sys.setrecursionlimit(10000)
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True, processes=1)
