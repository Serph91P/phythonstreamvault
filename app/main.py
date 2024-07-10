from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, abort, current_app
from flask_login import login_required, current_user
from app import db
from app.models import Streamer, User
from app.twitch_api import add_streamer_task, list_all_subscriptions, delete_all_subscriptions, subscribe_to_events
import hmac
import hashlib
import asyncio
from celery import shared_task

main = Blueprint('main', __name__)

@main.route('/')
def index():
    if not User.query.first():
        return redirect(url_for('auth.setup'))
    if not current_user.is_authenticated:
        return redirect(url_for('auth.login'))
    return render_template('index.html', title='Home')

@main.route('/dashboard')
@login_required
def dashboard():
    streamers = Streamer.query.filter_by(user_id=current_user.id).all()
    return render_template('dashboard.html', title='Dashboard', streamers=streamers)

@main.route('/add_streamer', methods=['POST'])
@login_required
def add_streamer():
    username = request.form.get('username')
    if username:
        try:
            task = add_streamer_task.delay(username, current_user.id)
            return jsonify({'task_id': str(task.id), 'status': 'Adding streamer...'}), 202
        except Exception as e:
            current_app.logger.error(f"Error adding streamer: {str(e)}")
            return jsonify({'error': 'An error occurred. Please try again later.'}), 500
    return jsonify({'error': 'No username provided'}), 400

@main.route('/task_status/<task_id>')
@login_required
def task_status(task_id):
    task = add_streamer_task.AsyncResult(task_id)
    if task.state == 'PENDING':
        response = {
            'state': task.state,
            'status': 'Adding streamer...'
        }
    elif task.state == 'SUCCESS':
        response = {
            'state': task.state,
            'result': task.result
        }
    else:
        response = {
            'state': task.state,
            'status': str(task.info)
        }
    return jsonify(response)

@main.route('/delete_streamer/<int:streamer_id>', methods=['POST'])
@login_required
def delete_streamer(streamer_id):
    streamer = Streamer.query.get_or_404(streamer_id)
    if streamer.user_id != current_user.id:
        abort(403)
    
    db.session.delete(streamer)
    db.session.commit()
    return jsonify({'status': 'success', 'message': 'Streamer has been deleted'})

@main.route('/webhook/callback', methods=['POST'])
def twitch_webhook_callback():
    # Verify Twitch signature
    twitch_signature = request.headers.get('Twitch-Eventsub-Message-Signature')
    message_id = request.headers.get('Twitch-Eventsub-Message-Id')
    timestamp = request.headers.get('Twitch-Eventsub-Message-Timestamp')
    message = message_id + timestamp + request.data.decode('utf-8')
    
    if not twitch_signature:
        current_app.logger.error("Twitch-Eventsub-Message-Signature header is missing")
        abort(400)
    
    expected_signature = 'sha256=' + hmac.new(
        current_app.config['TWITCH_WEBHOOK_SECRET'].encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    
    if not hmac.compare_digest(twitch_signature, expected_signature):
        current_app.logger.error("Invalid Twitch signature")
        abort(403)
    
    # Handle the event
    event_type = request.headers.get('Twitch-Eventsub-Message-Type')
    if event_type == 'webhook_callback_verification':
        challenge = request.json['challenge']
        return challenge, 200, {'Content-Type': 'text/plain'}
    elif event_type == 'notification':
        # Process the event
        event_data = request.json
        current_app.logger.info(f"Received event: {event_data}")
        # ... process the event ...
        return '', 204
    else:
        current_app.logger.error(f"Unknown event type: {event_type}")
        abort(400)

@main.route('/manage_subscriptions', methods=['GET'])
@login_required
def manage_subscriptions_page():
    return render_template('manage_subscriptions.html')

@main.route('/api/list_subscriptions', methods=['GET'])
@login_required
def api_list_subscriptions():
    async def async_list():
        subscriptions = await list_all_subscriptions()
        if not subscriptions:
            return {"message": "No active subscriptions found"}
        return [{"id": sub.id, "type": sub.type, "status": sub.status} for sub in subscriptions]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(async_list())
    finally:
        loop.close()
    return jsonify(result)

@main.route('/api/delete_subscriptions', methods=['POST'])
@login_required
def api_delete_subscriptions():
    async def async_delete():
        return await delete_all_subscriptions()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(async_delete())
    finally:
        loop.close()
    return jsonify({"message": result})

@shared_task
def background_resubscribe():
    from app import create_app
    
    app = create_app()
    
    async def async_resubscribe():
        with app.app_context():
            streamers = Streamer.query.all()
            results = []
            for streamer in streamers:
                try:
                    success = await subscribe_to_events(streamer, app)
                    if success:
                        results.append(f"Resubscribed to {streamer.username}")
                    else:
                        results.append(f"Failed to resubscribe to {streamer.username}")
                except Exception as e:
                    results.append(f"Error resubscribing to {streamer.username}: {str(e)}")
            return results

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(async_resubscribe())
    finally:
        loop.close()

@main.route('/api/resubscribe_all', methods=['POST'])
@login_required
def resubscribe_all():
    task = background_resubscribe.delay()
    return jsonify({"message": "Resubscribe process started", "task_id": str(task.id)}), 202

@main.route('/api/resubscribe_status/<task_id>', methods=['GET'])
@login_required
def resubscribe_status(task_id):
    task = background_resubscribe.AsyncResult(task_id)
    if task.state == 'PENDING':
        response = {
            'state': task.state,
            'status': 'Resubscribe process is pending...'
        }
    elif task.state != 'FAILURE':
        response = {
            'state': task.state,
            'status': task.info
        }
    else:
        response = {
            'state': task.state,
            'status': str(task.info)
        }
    return jsonify(response)