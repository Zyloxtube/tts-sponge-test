# app.py
from flask import Flask, request, jsonify
from flask_cors import CORS
import asyncio
import threading
import uuid
from playwright.async_api import async_playwright
import nest_asyncio
from datetime import datetime
import os

app = Flask(__name__)
CORS(app)

nest_asyncio.apply()

# Store job status
jobs = {}

async def generate_voiceover(text, job_id):
    """Async function to generate voiceover"""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
            )
            context = await browser.new_context()
            page = await context.new_page()
            
            # Update job status
            jobs[job_id]['status'] = 'processing'
            
            # Navigate to page
            await page.goto("https://nicevoice.org/ai-voice-generator/spongebob-squarepants/", wait_until="networkidle")
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

def run_async_task(text, job_id):
    """Run async task in new event loop"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(generate_voiceover(text, job_id))
    loop.close()

@app.route('/generate', methods=['GET'])
def generate():
    """Generate SpongeBob voiceover"""
    text = request.args.get('text', '').strip()
    
    if not text:
        return jsonify({
            'success': False,
            'error': 'No text provided. Please add ?text=your_text_here'
        }), 400
    
    # Create job
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        'status': 'pending',
        'text': text,
        'created_at': datetime.now().isoformat()
    }
    
    # Start generation in background thread
    thread = threading.Thread(target=run_async_task, args=(text, job_id))
    thread.daemon = True
    thread.start()
    
    return jsonify({
        'success': True,
        'job_id': job_id,
        'message': 'Voice generation started',
        'status_url': f'/status?job_id={job_id}'
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
            'created_at': job['created_at'],
            'completed_at': job.get('completed_at')
        })
    elif job['status'] == 'failed':
        return jsonify({
            'success': False,
            'status': 'failed',
            'error': job.get('error', 'Unknown error'),
            'text': job['text']
        }), 500
    else:
        return jsonify({
            'success': True,
            'status': job['status'],
            'message': 'Still processing... check back soon',
            'text': job['text']
        })

@app.route('/generate-and-wait', methods=['GET'])
def generate_and_wait():
    """Generate voiceover and wait for completion (synchronous)"""
    text = request.args.get('text', '').strip()
    
    if not text:
        return jsonify({
            'success': False,
            'error': 'No text provided. Please add ?text=your_text_here'
        }), 400
    
    # Run synchronously (will block until complete)
    job_id = str(uuid.uuid4())
    jobs[job_id] = {
        'status': 'pending',
        'text': text,
        'created_at': datetime.now().isoformat()
    }
    
    # Run the async function synchronously
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(generate_voiceover(text, job_id))
    loop.close()
    
    job = jobs[job_id]
    
    if job['status'] == 'completed':
        return jsonify({
            'success': True,
            'audio_url': job['audio_url'],
            'text': text
        })
    else:
        return jsonify({
            'success': False,
            'error': job.get('error', 'Generation failed')
        }), 500

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'active_jobs': len([j for j in jobs.values() if j['status'] == 'processing'])
    })

if __name__ == '__main__':
    # Install playwright browsers if needed
    os.system('playwright install chromium')
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
