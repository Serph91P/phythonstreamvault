from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, abort
from flask_login import login_required, current_user
from app import db
from app.models import Streamer, User
from app.twitch_api import subscribe_to_events
import hmac
import hashlib
import asyncio

main = Blueprint('main', __name__)

@main.route('/')
def index():
    if not User.query.first():
        return redirect(url_for('auth.setup'))
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
        from app.twitch_api import add_streamer as twitch_add_streamer
        twitch_add_streamer(username, current_user.id)
        flash('Streamer added successfully', 'success')
    return redirect(url_for('main.dashboard'))

@main.route('/webhook', methods=['POST'])
def twitch_webhook():
    # Überprüfen der Twitch-Signatur
    twitch_signature = request.headers.get('Twitch-Eventsub-Message-Signature')
    if not twitch_signature:
        abort(400, description="Twitch-Eventsub-Message-Signature header is missing")

    message = request.headers.get('Twitch-Eventsub-Message-Id', '') + \
              request.headers.get('Twitch-Eventsub-Message-Timestamp', '') + \
              request.data.decode('utf-8')

    from flask import current_app
    secret = current_app.config['TWITCH_WEBHOOK_SECRET'].encode('utf-8')
    expected_signature = 'sha256=' + hmac.new(secret, message.encode('utf-8'), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(twitch_signature, expected_signature):
        abort(403, description="Invalid signature")

    # Verarbeiten des Ereignisses
    event_type = request.headers.get('Twitch-Eventsub-Message-Type')
    event_data = request.json

    if event_type == 'webhook_callback_verification':
        return jsonify({'challenge': event_data['challenge']})

    if event_type == 'notification':
        subscription_type = event_data['subscription']['type']
        event = event_data['event']
        broadcaster_id = event['broadcaster_user_id']

        streamer = Streamer.query.filter_by(twitch_id=broadcaster_id).first()
        if not streamer:
            abort(404, description="Streamer not found")

        if subscription_type == 'stream.online':
            streamer.is_live = True
        elif subscription_type == 'stream.offline':
            streamer.is_live = False
        elif subscription_type == 'channel.update':
            streamer.stream_title = event.get('title')
            streamer.game_name = event.get('category_name')

        db.session.commit()

    return jsonify(success=True), 200