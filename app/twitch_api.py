import asyncio
import traceback
from flask import current_app
from twitchAPI.twitch import Twitch
from twitchAPI.eventsub.webhook import EventSubWebhook
from twitchAPI.oauth import UserAuthenticator
from twitchAPI.object.eventsub import ChannelUpdateEvent, StreamOfflineEvent, StreamOnlineEvent
from twitchAPI.helper import first
from app import celery

twitch = None
eventsub = None

SUBSCRIPTION_TIMEOUT = 120


async def setup_twitch(app):
    global twitch
    try:
        current_app.logger.info(f"Initializing Twitch API with CLIENT_ID: {app.config['TWITCH_CLIENT_ID']}")
        twitch = await Twitch(app.config['TWITCH_CLIENT_ID'], app.config['TWITCH_CLIENT_SECRET'])
        await twitch.authenticate_app([])
        twitch.webhook_secret = app.config['TWITCH_WEBHOOK_SECRET']
        current_app.logger.info("Twitch API initialized successfully")
        return twitch
    except Exception as e:
        current_app.logger.error(f"Failed to initialize Twitch API: {str(e)}")
        current_app.logger.error(traceback.format_exc())
        return None

async def setup_eventsub(app, twitch_instance):
    global eventsub
    try:
        current_app.logger.info(f"Initializing EventSubWebhook with CALLBACK_URL: {app.config['CALLBACK_URL']}")
        current_app.logger.info(f"Twitch instance: {twitch_instance}")
        eventsub = EventSubWebhook(
            callback_url=app.config['CALLBACK_URL'],
            port=8080,
            twitch=twitch_instance
        )
        current_app.logger.info(f"EventSubWebhook object created: {eventsub}")
        
        if eventsub is None:
            current_app.logger.error("EventSubWebhook initialization failed: eventsub is None")
            return None
        
        current_app.logger.info("Starting EventSubWebhook...")
        await eventsub.start()
        current_app.logger.info("EventSubWebhook started successfully")
        return eventsub
    except Exception as e:
        current_app.logger.error(f"Failed to initialize or start EventSub: {str(e)}")
        current_app.logger.error(traceback.format_exc())
        return None

async def ensure_twitch_initialized(app):
    global twitch, eventsub
    if twitch is None:
        current_app.logger.info("Twitch API not initialized. Initializing now...")
        twitch = await setup_twitch(app)
    if twitch is None:
        current_app.logger.error("Failed to initialize Twitch API")
        return None, None
    
    current_app.logger.info("Twitch API initialized successfully")
    
    if eventsub is None:
        current_app.logger.info("EventSub not initialized. Initializing now...")
        eventsub = await setup_eventsub(app, twitch)
    if eventsub is None:
        current_app.logger.error("Failed to initialize EventSub")
        return twitch, None
    
    current_app.logger.info("EventSub initialized successfully")
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
            current_app.logger.error("Failed to initialize Twitch API or EventSub")
            return False

        user = await first(twitch.get_users(logins=[streamer.username]))
        if user:
            streamer.twitch_id = user.id
            from app import db
            db.session.commit()
            
            current_app.logger.info(f"Subscribing to events for user {user.id}")

            subscription_functions = [
                (eventsub.listen_stream_online, "stream.online"),
                (eventsub.listen_stream_offline, "stream.offline"),
                (eventsub.listen_channel_update, "channel.update")
            ]

            for subscribe_func, event_type in subscription_functions:
                max_attempts = 3
                for attempt in range(max_attempts):
                    try:
                        current_app.logger.info(f"Attempting to subscribe to {event_type} event (Attempt {attempt + 1}/{max_attempts})")
                        subscription = await asyncio.wait_for(subscribe_func(user.id, globals()[f"on_{event_type.replace('.', '_')}"]), timeout=SUBSCRIPTION_TIMEOUT)
                        current_app.logger.info(f"Successfully subscribed to {event_type} event: {subscription.id}")
                        break
                    except asyncio.TimeoutError:
                        current_app.logger.error(f"Timeout while subscribing to {event_type} event")
                    except Exception as e:
                        if "already subscribed" in str(e).lower():
                            current_app.logger.warning(f"Subscription for {event_type} already exists. Attempting to delete and resubscribe.")
                            await delete_all_subscriptions()
                            continue
                        else:
                            current_app.logger.error(f"Failed to subscribe to {event_type} event: {str(e)}")
                    
                    if attempt == max_attempts - 1:
                        current_app.logger.error(f"Failed to subscribe to {event_type} event after {max_attempts} attempts")
                        return False

            return True
        else:
            current_app.logger.error(f"User not found for username: {streamer.username}")
            return False
    except Exception as e:
        current_app.logger.error(f"Error in subscribe_to_events: {str(e)}")
        current_app.logger.error(traceback.format_exc())
        return False

@celery.task
def add_streamer_task(username, user_id):
    from app import create_app
    
    app = create_app()
    
    async def run_task():
        twitch, eventsub = await ensure_twitch_initialized(app)
        if twitch is None or eventsub is None:
            return False
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
            current_app.logger.info(f"Streamer {streamer.username} is now live")

async def on_stream_offline(data: dict):
    app = current_app._get_current_object()
    with app.app_context():
        from app.models import Streamer
        from app import db
        streamer = Streamer.query.filter_by(twitch_id=data['event']['broadcaster_user_id']).first()
        if streamer:
            streamer.is_live = False
            db.session.commit()
            current_app.logger.info(f"Streamer {streamer.username} is now offline")

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
            current_app.logger.info(f"Channel updated for streamer {streamer.username}")