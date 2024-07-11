import asyncio
import threading
import traceback
from flask import current_app
from twitchAPI.twitch import Twitch
from twitchAPI.eventsub.webhook import EventSubWebhook
from twitchAPI.helper import first
from app import celery
from app import db
from app.models import Streamer

twitch = None
eventsub = None
eventsub_init_lock = threading.Lock()

async def setup_twitch(app):
    global twitch
    try:
        app.logger.info(f"Initializing Twitch API with CLIENT_ID: {app.config['TWITCH_CLIENT_ID']}")
        twitch = await Twitch(app.config['TWITCH_CLIENT_ID'], app.config['TWITCH_CLIENT_SECRET'])
        await twitch.authenticate_app([])
        app.logger.info("Twitch API initialized successfully")
        return twitch
    except Exception as e:
        app.logger.error(f"Failed to initialize Twitch API: {str(e)}")
        app.logger.error(traceback.format_exc())
        return None

def setup_and_start_eventsub(app, twitch_instance):
    global eventsub
    try:
        callback_url = app.config['CALLBACK_URL']
        secret = app.config['TWITCH_WEBHOOK_SECRET']
        port = int(app.config['EVENTSUB_WEBHOOK_PORT'])
        
        app.logger.info(f"Initializing EventSubWebhook with CALLBACK_URL: {callback_url}")
        app.logger.info(f"Using port: {port}")
        
        eventsub = EventSubWebhook(callback_url, port, secret, twitch_instance)
        app.logger.info(f"EventSubWebhook instance created: {eventsub}")
        
        # Start the EventSubWebhook in a separate thread
        def start_eventsub():
            eventsub.start()

        threading.Thread(target=start_eventsub, daemon=True).start()
        app.logger.info("EventSubWebhook starting in background thread")
        
        return eventsub
    except Exception as e:
        app.logger.error(f"Failed to initialize or start EventSub: {str(e)}")
        app.logger.error(traceback.format_exc())
        return None

async def ensure_twitch_initialized(app):
    global twitch, eventsub
    try:
        if twitch is None:
            app.logger.info(f"Initializing Twitch API with CLIENT_ID: {app.config['TWITCH_CLIENT_ID']}")
            twitch = await Twitch(app.config['TWITCH_CLIENT_ID'], app.config['TWITCH_CLIENT_SECRET'])
            await twitch.authenticate_app([])
            app.logger.info("Twitch API initialized successfully")
        
        with eventsub_init_lock:
            if eventsub is None:
                app.logger.info("Starting EventSub initialization...")
                callback_url = app.config['CALLBACK_URL']
                secret = app.config['TWITCH_WEBHOOK_SECRET']
                port = int(app.config['EVENTSUB_WEBHOOK_PORT'])
                
                app.logger.info(f"Initializing EventSubWebhook with CALLBACK_URL: {callback_url}")
                eventsub = EventSubWebhook(callback_url, port, secret, twitch)
                
                def start_eventsub():
                    eventsub.start()
                
                threading.Thread(target=start_eventsub, daemon=True).start()
                app.logger.info("EventSubWebhook starting in background thread")
            else:
                app.logger.info("EventSub already initialized")
        
        return twitch, eventsub
    except Exception as e:
        app.logger.error(f"Failed to initialize Twitch API or EventSub: {str(e)}")
        app.logger.error(traceback.format_exc())
        return None, None

async def list_all_subscriptions():
    try:
        if twitch is None:
            current_app.logger.error("Twitch API not initialized")
            return []
        result = await twitch.get_eventsub_subscriptions()
        subscriptions = result.data if result else []
        current_app.logger.info(f"All current subscriptions: {subscriptions}")
        return subscriptions
    except Exception as e:
        current_app.logger.error(f"Error listing subscriptions: {str(e)}")
        current_app.logger.error(traceback.format_exc())
        return []

async def delete_all_subscriptions():
    try:
        if twitch is None:
            current_app.logger.error("Twitch API not initialized")
            return "Twitch API not initialized"
        subscriptions = await list_all_subscriptions()
        for sub in subscriptions:
            try:
                await twitch.delete_eventsub_subscription(sub.id)
                current_app.logger.info(f"Deleted subscription: {sub.id}")
            except Exception as e:
                current_app.logger.error(f"Failed to delete subscription {sub.id}: {str(e)}")
        return "All subscriptions deleted"
    except Exception as e:
        current_app.logger.error(f"Error deleting subscriptions: {str(e)}")
        current_app.logger.error(traceback.format_exc())
        return "Error deleting subscriptions"

async def subscribe_to_events(streamer_or_username, app):
    global twitch, eventsub
    try:
        twitch, eventsub = await ensure_twitch_initialized(app)
        if twitch is None or eventsub is None:
            app.logger.error("Failed to initialize Twitch API or EventSub")
            return False

        if isinstance(streamer_or_username, str):
            username = streamer_or_username
            streamer = Streamer.query.filter_by(username=username).first()
            if streamer is None:
                streamer = Streamer(username=username)
                db.session.add(streamer)
                db.session.commit()
        else:
            streamer = streamer_or_username

        user = await first(twitch.get_users(logins=[streamer.username]))
        if user:
            streamer.twitch_id = int(user.id)
            db.session.commit()

            app.logger.info(f"Subscribing to events for user {user.id}")

            try:
                await eventsub.listen_channel_update(user.id, on_channel_update)
                await eventsub.listen_stream_online(user.id, on_stream_online)
                await eventsub.listen_stream_offline(user.id, on_stream_offline)
                app.logger.info(f"Successfully subscribed to events for {streamer.username}")
            except Exception as e:
                app.logger.error(f"Failed to subscribe to events: {str(e)}")
                return False

            return True
        else:
            app.logger.error(f"User not found for username: {streamer.username}")
            return False
    except Exception as e:
        app.logger.error(f"Error in subscribe_to_events: {str(e)}")
        app.logger.error(traceback.format_exc())
        return False

@celery.task
def add_streamer_task(username, user_id):
    from app import create_app
    
    app = create_app()
    
    with app.app_context():
        async def run_task():
            return await subscribe_to_events(username, app)
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(run_task())
        except Exception as e:
            app.logger.error(f"Error in add_streamer_task: {str(e)}")
            app.logger.error(traceback.format_exc())
            result = False
        finally:
            loop.close()
    
    return result

async def on_stream_online(data: dict):
    app = current_app._get_current_object()
    with app.app_context():
        from app.models import Streamer
        from app import db
        streamer = Streamer.query.filter_by(twitch_id=int(data['event']['broadcaster_user_id'])).first()
        if streamer:
            streamer.is_live = True
            db.session.commit()
            app.logger.info(f"Streamer {streamer.username} is now live")

async def on_stream_offline(data: dict):
    app = current_app._get_current_object()
    with app.app_context():
        from app.models import Streamer
        from app import db
        streamer = Streamer.query.filter_by(twitch_id=int(data['event']['broadcaster_user_id'])).first()
        if streamer:
            streamer.is_live = False
            db.session.commit()
            app.logger.info(f"Streamer {streamer.username} is now offline")

async def on_channel_update(data: dict):
    app = current_app._get_current_object()
    with app.app_context():
        from app.models import Streamer
        from app import db
        streamer = Streamer.query.filter_by(twitch_id=int(data['event']['broadcaster_user_id'])).first()
        if streamer:
            streamer.stream_title = data['event']['title']
            streamer.game_name = data['event']['category_name']
            db.session.commit()
            app.logger.info(f"Channel updated for streamer {streamer.username}")