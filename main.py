# main.py
from flask import Flask, request, jsonify
from flask_cors import CORS
import asyncio
import uuid
from playwright.async_api import async_playwright
import nest_asyncio
from datetime import datetime
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import gc

# Try to import psutil for memory monitoring, but don't fail if not available
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    print("Warning: psutil not installed. Install it with 'pip install psutil' for memory monitoring")

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

# Create thread pool for handling multiple requests
executor = ThreadPoolExecutor(max_workers=600)  # Allow up to 600 concurrent tasks

# Semaphore to control concurrent browser instances
# MAX 500 CONCURRENT BROWSERS
browser_semaphore = asyncio.Semaphore(500)  # Allow 500 concurrent browsers

# Track active browser count
active_browsers = 0
browser_lock = threading.Lock()

async def generate_single_voiceover(text, job_id, character='spongebob'):
    """Generate a single voiceover with semaphore control (max 500 concurrent)"""
    global active_browsers
    
    async with browser_semaphore:  # Limit to 500 concurrent browsers
        with browser_lock:
            active_browsers += 1
            current_active = active_browsers
            
        print(f"[{datetime.now().strftime('%H:%M:%S')}] 🔄 Job {job_id[:8]} started. Active browsers: {current_active}/500")
        
        try:
            character = character.lower().replace(' ', '')
            if character not in CHARACTER_URLS:
                character = 'spongebob'
            
            voice_url = CHARACTER_URLS[character]
            
            async with async_playwright() as p:
                # Launch with optimized settings for maximum concurrency
                browser = await p.chromium.launch(
                    headless=True,
                    args=[
                        '--no-sandbox', 
                        '--disable-setuid-sandbox', 
                        '--disable-dev-shm-usage',
                        '--disable-gpu',
                        '--disable-software-rasterizer',
                        '--disable-extensions',
                        '--disable-background-timer-throttling',
                        '--disable-backgrounding-occluded-windows',
                        '--disable-renderer-backgrounding',
                        '--disable-ipc-flooding-protection',
                        '--disable-hang-monitor',
                        '--disable-prompt-on-repost',
                        '--disable-sync',
                        '--disable-web-security',
                        '--aggressive-cache-discard',
                        '--disable-cache',
                        '--disable-application-cache',
                        '--disable-offline-load-stale-cache',
                        '--disable-gpu-shader-disk-cache',
                        '--media-cache-size=0',
                        '--disk-cache-size=0'
                    ]
                )
                
                # Create context with minimal overhead
                context = await browser.new_context(
                    viewport={'width': 400, 'height': 300},  # Smaller viewport saves memory
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                    java_script_enabled=True,
                    bypass_csp=True,
                    ignore_https_errors=True
                )
                
                page = await context.new_page()
                
                # Set shorter timeouts for better concurrency
                page.set_default_timeout(30000)
                
                # Update job status
                with job_lock:
                    jobs[job_id]['status'] = 'processing'
                    jobs[job_id]['character'] = character
                
                # Navigate to character page with timeout
                try:
                    await page.goto(voice_url, wait_until="domcontentloaded", timeout=30000)
                    await asyncio.sleep(0.5)  # Reduced sleep time
                except Exception as e:
                    print(f"Navigation error for {job_id}: {e}")
                    raise
                
                # Type text in textarea
                textarea = await page.query_selector('textarea.textarea')
                if textarea:
                    await textarea.fill(text)
                
                # Click generate button
                generate_button = await page.query_selector('button.btn-primary:has-text("Generate Voiceover")')
                if generate_button:
                    await generate_button.click()
                
                # Wait for audio URL - checking frequently
                audio_url = None
                attempts = 0
                max_attempts = 120  # 60 seconds total (reduced from 90)
                
                while attempts < max_attempts:
                    audio_element = await page.query_selector('audio[src*=".mp3"]')
                    if audio_element:
                        audio_url = await audio_element.get_attribute('src')
                        if audio_url and audio_url.startswith('http'):
                            break
                    await asyncio.sleep(0.3)  # Check more frequently
                    attempts += 1
                
                await browser.close()
                
                # Force garbage collection periodically
                if int(job_id[:2], 16) % 50 == 0:  # Every ~50 jobs
                    gc.collect()
                
                if audio_url:
                    with job_lock:
                        jobs[job_id]['status'] = 'completed'
                        jobs[job_id]['audio_url'] = audio_url
                        jobs[job_id]['completed_at'] = datetime.now().isoformat()
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ✅ Job {job_id[:8]} completed")
                else:
                    with job_lock:
                        jobs[job_id]['status'] = 'failed'
                        jobs[job_id]['error'] = 'Timeout: Audio generation took too long'
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Job {job_id[:8]} timed out")
                    
        except Exception as e:
            with job_lock:
                jobs[job_id]['status'] = 'failed'
                jobs[job_id]['error'] = str(e)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] ❌ Job {job_id[:8]} failed: {str(e)[:100]}")
        
        finally:
            with browser_lock:
                active_browsers -= 1
                current_active = active_browsers
            print(f"[{datetime.now().strftime('%H:%M:%S')}] 📊 Active browsers: {current_active}/500")

async def generate_multiple_voiceovers(texts_and_characters):
    """Generate multiple voiceovers concurrently (max 500 at once)"""
    tasks = []
    
    # Create all tasks
    for text, character in texts_and_characters:
        job_id = str(uuid.uuid4())
        with job_lock:
            jobs[job_id] = {
                'status': 'pending',
                'text': text,
                'character': character,
                'created_at': datetime.now().isoformat()
            }
        tasks.append(generate_single_voiceover(text, job_id, character))
    
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 🚀 Starting {len(tasks)} concurrent voice generations (max 500)")
    
    # Run all tasks concurrently (semaphore will limit to 500)
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Count successes and failures
    success_count = sum(1 for r in results if r is None)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 📈 Batch complete: {success_count}/{len(tasks)} successful")
    
    return results

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

@app.route('/generate-batch', methods=['POST'])
def generate_batch():
    """Generate multiple voiceovers at once (max 2000 per batch)"""
    data = request.json
    
    if not data or 'requests' not in data:
        return jsonify({
            'success': False,
            'error': 'Please provide a list of requests with text and character'
        }), 400
    
    requests_list = data['requests']
    
    if len(requests_list) > 2000:
        return jsonify({
            'success': False,
            'error': 'Maximum 2000 requests per batch'
        }), 400
    
    # Prepare texts and characters
    texts_and_characters = []
    job_ids = []
    
    for req in requests_list:
        text = req.get('text', '').strip()
        character = req.get('character', 'spongebob').strip()
        
        if not text:
            continue
            
        if character.lower() not in CHARACTER_URLS:
            character = 'spongebob'
            
        texts_and_characters.append((text, character))
        job_id = str(uuid.uuid4())
        job_ids.append(job_id)
        
        with job_lock:
            jobs[job_id] = {
                'status': 'pending',
                'text': text,
                'character': character,
                'created_at': datetime.now().isoformat()
            }
    
    # Run async generation in background
    def run_async_batch():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(generate_multiple_voiceovers(texts_and_characters))
        loop.close()
    
    # Start batch processing in background
    thread = threading.Thread(target=run_async_batch)
    thread.daemon = True
    thread.start()
    
    return jsonify({
        'success': True,
        'batch_id': str(uuid.uuid4()),
        'job_ids': job_ids,
        'total_jobs': len(job_ids),
        'max_concurrent': 500,
        'message': f'Started {len(job_ids)} voice generations (max 500 running at once)',
        'estimated_time': f'~{max(30, len(job_ids) // 10)} seconds'
    })

@app.route('/generate-500-sounds', methods=['POST'])
def generate_500_sounds():
    """Convenience endpoint to generate exactly 500 sounds"""
    data = request.json
    
    if not data or 'texts' not in data:
        return jsonify({
            'success': False,
            'error': 'Please provide 500 texts in the "texts" array'
        }), 400
    
    texts = data['texts']
    character = data.get('character', 'spongebob')
    
    if len(texts) != 500:
        return jsonify({
            'success': False,
            'error': f'Please provide exactly 500 texts, got {len(texts)}'
        }), 400
    
    # Create 500 requests
    requests_list = [{'text': text, 'character': character} for text in texts]
    
    return generate_batch()

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
    
    # Run single generation in background
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
        'max_concurrent': 500,
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

@app.route('/batch-status', methods=['POST'])
def batch_status():
    """Get status for multiple jobs at once"""
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
def get_stats():
    """Get system statistics"""
    with job_lock:
        total_jobs = len(jobs)
        completed_jobs = len([j for j in jobs.values() if j['status'] == 'completed'])
        failed_jobs = len([j for j in jobs.values() if j['status'] == 'failed'])
        processing_jobs = len([j for j in jobs.values() if j['status'] == 'processing'])
        pending_jobs = len([j for j in jobs.values() if j['status'] == 'pending'])
    
    with browser_lock:
        current_browsers = active_browsers
    
    # Get system memory info
    if PSUTIL_AVAILABLE:
        try:
            memory = psutil.virtual_memory()
            memory_usage = {
                'total_gb': round(memory.total / (1024**3), 2),
                'available_gb': round(memory.available / (1024**3), 2),
                'percent_used': memory.percent
            }
        except:
            memory_usage = {'error': 'Could not get memory info'}
    else:
        memory_usage = {'error': 'psutil not installed'}
    
    return jsonify({
        'success': True,
        'jobs': {
            'total': total_jobs,
            'completed': completed_jobs,
            'failed': failed_jobs,
            'processing': processing_jobs,
            'pending': pending_jobs
        },
        'concurrent': {
            'max_allowed': 500,
            'currently_active': current_browsers,
            'available_slots': 500 - current_browsers
        },
        'system_memory': memory_usage,
        'timestamp': datetime.now().isoformat()
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
    
    with browser_lock:
        current_browsers = active_browsers
    
    return jsonify({
        'status': 'healthy',
        'active_jobs': active_jobs,
        'max_concurrent': 500,
        'active_browsers': current_browsers,
        'total_jobs_processed': len(jobs)
    })

if __name__ == '__main__':
    # Install playwright browsers if needed
    os.system('playwright install chromium')
    
    print("=" * 60)
    print("🚀 Voice Generation Server Starting")
    print("=" * 60)
    print(f"📊 Max Concurrent Jobs: 500")
    print(f"💾 Recommended RAM: 32GB+")
    print(f"🔥 Ready to handle massive concurrent requests!")
    print(f"🏓 Ping endpoint: http://localhost:5000/ping")
    print("=" * 60)
    
    # Run with multiple workers for better concurrency
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True, processes=4)
