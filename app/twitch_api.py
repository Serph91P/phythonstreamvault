import asyncio
import threading
import traceback
from flask import current_app
from twitchAPI.twitch import Twitch
from twitchAPI.eventsub.webhook import EventSubWebhook
from twitchAPI.helper import first
from app import celery

twitch = None
eventsub = None
eventsub_init_lock = threading.Lock()

async def setup_twitch(app):
    global twitch
    try:
        app.logger.info(f"Initializing Twitch API with CLIENT_ID: {app.config['TWITCH_CLIENT_ID']}")
        twitch = await Twitch(app.config['TWITCH_CLIENT_ID'], app.config['TWITCH_CLIENT_SECRET'])
        await twitch.authenticate_app([])
        twitch.webhook_secret = app.config['TWITCH_WEBHOOK_SECRET']
        app.logger.info("Twitch API initialized successfully")
        return twitch
    except Exception as e:
        app.logger.error(f"Failed to initialize Twitch API: {str(e)}")
        app.logger.error(traceback.format_exc())
        return None

async def setup_and_start_eventsub(app, twitch_instance):
    global eventsub
    try:
        app.logger.info(f"Initializing EventSubWebhook with CALLBACK_URL: {app.config['CALLBACK_URL']}")
        eventsub = EventSubWebhook(
            app.config['CALLBACK_URL'],
            app.config['EVENTSUB_WEBHOOK_PORT'],
            twitch_instance,
            ssl_context=None,  # Using reverse proxy for SSL
            host_binding='0.0.0.0'
        )
        app.logger.info(f"EventSubWebhook instance created: {eventsub}")
        await eventsub.start()
        app.logger.info("EventSubWebhook started successfully")
    except Exception as e:
        app.logger.error(f"Failed to initialize or start EventSub: {str(e)}")
        app.logger.error(traceback.format_exc())
        eventsub = None

def start_eventsub_in_thread(app, twitch_instance):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(setup_and_start_eventsub(app, twitch_instance))

async def ensure_twitch_initialized(app):
    global twitch, eventsub
    if twitch is None:
        twitch = await setup_twitch(app)
    if twitch is None:
        app.logger.error("Failed to initialize Twitch API")
        return None, None
    
    with eventsub_init_lock:
        if eventsub is None:
            app.logger.info("Starting EventSub initialization...")
            thread = threading.Thread(target=start_eventsub_in_thread, args=(app, twitch))
            thread.start()
            for i in range(30):  # Wait up to 30 seconds
                if eventsub is not None:
                    app.logger.info(f"EventSub initialized after {i} seconds")
                    break
                await asyncio.sleep(1)
            else:
                app.logger.error("Timed out waiting for EventSub to initialize")
        else:
            app.logger.info("EventSub already initialized")
    
    return twitch, eventsub

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

async def subscribe_to_events(streamer, app):
    global twitch, eventsub
    try:
        twitch, eventsub = await ensure_twitch_initialized(app)
        if twitch is None or eventsub is None:
            app.logger.error("Failed to initialize Twitch API or EventSub")
            return False

        user = await first(twitch.get_users(logins=[streamer.username]))
        if user:
            streamer.twitch_id = user.id
            from app import db
            db.session.commit()
            
            app.logger.info(f"Subscribing to events for user {user.id}")

            subscription_types = ["stream.online", "stream.offline", "channel.update"]
            for event_type in subscription_types:
                try:
                    # Check if subscription already exists
                    existing_subs = await twitch.get_eventsub_subscriptions()
                    sub_exists = any(sub.type == event_type and sub.condition.get('broadcaster_user_id') == str(user.id) for sub in existing_subs.data)
                    
                    if sub_exists:
                        app.logger.info(f"Subscription for {event_type} already exists. Skipping.")
                        continue

                    # If not exists, create new subscription
                    if eventsub is not None:
                        subscription = await getattr(eventsub, f"listen_{event_type.replace('.', '_')}")(user.id, globals()[f"on_{event_type.replace('.', '_')}"])
                        app.logger.info(f"Successfully subscribed to {event_type} event: {subscription.id}")
                    else:
                        app.logger.error(f"Failed to subscribe to {event_type}: EventSub is not initialized")
                except Exception as e:
                    app.logger.error(f"Failed to subscribe to {event_type}: {str(e)}")

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
        finally:
            loop.close()
    
    return result

async def on_stream_online(data: dict):
    app = current_app._get_current_object()
    with app.app_context():
        from app.models import Streamer
        from app import db
        streamer = Streamer.query.filter_by(twitch_id=data['event']['broadcaster_user_id']).first()
        if streamer:
            streamer.is_live = True
            db.session.commit()
            app.logger.info(f"Streamer {streamer.username} is now live")

async def on_stream_offline(data: dict):
    app = current_app._get_current_object()
    with app.app_context():
        from app.models import Streamer
        from app import db
        streamer = Streamer.query.filter_by(twitch_id=data['event']['broadcaster_user_id']).first()
        if streamer:
            streamer.is_live = False
            db.session.commit()
            app.logger.info(f"Streamer {streamer.username} is now offline")

async def on_channel_update(data: dict):
    app = current_app._get_current_object()
    with app.app_context():
        from app.models import Streamer
        from app import db
        streamer = Streamer.query.filter_by(twitch_id=data['event']['broadcaster_user_id']).first()
        if streamer:
            streamer.stream_title = data['event']['title']
            streamer.game_name = data['event']['category_name']
            db.session.commit()
            app.logger.info(f"Channel updated for streamer {streamer.username}")
