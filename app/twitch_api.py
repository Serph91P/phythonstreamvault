import asyncio
import traceback
import inspect
from flask import current_app as app
from twitchAPI.twitch import Twitch
from twitchAPI.eventsub.webhook import EventSubWebhook
from twitchAPI.helper import first
from app import celery, create_app, db
from app.models import Streamer, TwitchEvent, User

twitch = None
eventsub = None
init_lock = asyncio.Lock()

async def setup_twitch(app):
    global twitch
    if twitch is not None:
        app.logger.info("Twitch API already initialized")
        return twitch
    try:
        app.logger.info(f"Initializing Twitch API with CLIENT_ID: {app.config['TWITCH_CLIENT_ID']}")
        app.logger.info(f"CLIENT_SECRET: {app.config['TWITCH_CLIENT_SECRET'][:5]}...")  # Log first 5 chars of secret
        twitch = await Twitch(app.config['TWITCH_CLIENT_ID'], app.config['TWITCH_CLIENT_SECRET'])
        app.logger.info("Twitch instance created, authenticating...")
        await twitch.authenticate_app([])
        app.logger.info("Twitch API authenticated successfully")
        return twitch
    except Exception as e:
        app.logger.error(f"Failed to initialize Twitch API: {str(e)}")
        app.logger.error(traceback.format_exc())
        return None

async def setup_eventsub(app, twitch_instance):
    global eventsub
    app.logger.info("Setting up EventSubWebhook")

    if eventsub is not None:
        app.logger.info("EventSubWebhook already initialized")
        return eventsub

    if twitch_instance is None:
        app.logger.error("Cannot setup EventSubWebhook: Twitch API not initialized")
        return None

    callback_url = app.config.get('CALLBACK_URL')
    secret = app.config.get('TWITCH_WEBHOOK_SECRET')
    port = app.config.get('EVENTSUB_WEBHOOK_PORT')

    app.logger.info(f"Initializing EventSubWebhook with CALLBACK_URL: {callback_url}")
    app.logger.info(f"Using port: {port}")
    app.logger.info(f"Secret: {secret[:5] if secret else 'Not set'}...")

    try:
        app.logger.info("Creating EventSubWebhook instance")
        app.logger.info(f"EventSubWebhook parameters: callback_url={callback_url}, port={port}, secret={secret[:5]}..., twitch_instance={twitch_instance}")
        eventsub = EventSubWebhook(callback_url, int(port), secret, twitch_instance)
        app.logger.info(f"EventSubWebhook instance created: {eventsub}")
    except Exception as e:
        app.logger.error(f"Failed to create EventSubWebhook instance: {str(e)}")
        app.logger.error(traceback.format_exc())
        return None

    if eventsub is None:
        app.logger.error("EventSubWebhook instance is None after creation")
        return None

    try:
        app.logger.info("Starting EventSubWebhook")
        await eventsub.start()
        app.logger.info(f"EventSubWebhook started on port {port}")
    except Exception as e:
        app.logger.error(f"Failed to start EventSubWebhook: {str(e)}")
        app.logger.error(traceback.format_exc())
        eventsub = None

    return eventsub

def check_eventsub_webhook():
    from twitchAPI.eventsub.webhook import EventSubWebhook
    if hasattr(EventSubWebhook, 'start'):
        app.logger.info("EventSubWebhook has a 'start' method.")
    else:
        app.logger.error("EventSubWebhook does NOT have a 'start' method.")

async def ensure_twitch_initialized(app):
    global twitch, eventsub
    app.logger.info("Ensuring Twitch API is initialized")
    async with init_lock:
        if twitch is None:
            app.logger.info("Twitch is None, initializing...")
            twitch = await setup_twitch(app)
            if twitch is None:
                app.logger.error("Failed to initialize Twitch API in setup_twitch")
                return None, None
            else:
                app.logger.info("Twitch API initialized successfully")
        else:
            app.logger.info("Twitch is already initialized")
        
        app.logger.info(f"Twitch API initialization status: {'Success' if twitch else 'Failed'}")
        
        if eventsub is None:
            app.logger.info("EventSub is None, initializing...")
            eventsub = await setup_eventsub(app, twitch)
            if eventsub is None:
                app.logger.error("Failed to initialize EventSub in setup_eventsub")
                return twitch, None
            else:
                app.logger.info("EventSub initialized successfully")
        else:
            app.logger.info("EventSub is already initialized")
    
    app.logger.info(f"Twitch initialization status: {'Success' if twitch else 'Failed'}")
    app.logger.info(f"EventSub initialization status: {'Success' if eventsub else 'Failed'}")
    return twitch, eventsub

def async_init(app):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        app.logger.info("Starting async initialization")
        twitch, eventsub = loop.run_until_complete(ensure_twitch_initialized(app))
        if twitch is None or eventsub is None:
            app.logger.error("Failed to initialize Twitch API or EventSub")
        else:
            app.logger.info("Async initialization completed successfully")
    except Exception as e:
        app.logger.error(f"Error in async_init: {str(e)}")
        app.logger.error(traceback.format_exc())
    finally:
        loop.close()

async def subscribe_to_events(streamer_or_username, app, user_id=None):
    global twitch, eventsub
    app.logger.debug("Entering subscribe_to_events function")
    twitch, eventsub = await ensure_twitch_initialized(app)
    if twitch is None or eventsub is None:
        app.logger.error("Failed to initialize Twitch API or EventSub")
        return False

    app.logger.debug(f"streamer_or_username: {streamer_or_username}, user_id: {user_id}")
    if isinstance(streamer_or_username, str):
        username = streamer_or_username
        app.logger.debug(f"Looking for existing streamer with username: {username}")
        streamer = Streamer.query.filter_by(username=username).first()
        if streamer is None:
            app.logger.debug("Streamer not found, creating new one")
            if user_id is None:
                app.logger.debug("user_id is None, attempting to get first user")
                try:
                    user = User.query.first()
                    if not user:
                        app.logger.error("No user found to associate with streamer")
                        return False
                    user_id = user.id
                except Exception as e:
                    app.logger.error(f"Error while querying User: {str(e)}")
                    app.logger.error(traceback.format_exc())
                    return False
            app.logger.debug(f"Creating new streamer with username: {username}, user_id: {user_id}")
            streamer = Streamer(username=username, user_id=user_id)
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
            app.logger.error(traceback.format_exc())
            return False

        return True
    else:
        app.logger.error(f"User not found for username: {streamer.username}")
        return False

async def list_all_subscriptions():
    global twitch, eventsub
    try:
        if twitch is None:
            app.logger.error("Twitch API not initialized")
            return []
        result = await twitch.get_eventsub_subscriptions()
        subscriptions = result.data if result else []
        app.logger.info(f"All current subscriptions: {subscriptions}")
        return subscriptions
    except Exception as e:
        app.logger.error(f"Error listing subscriptions: {str(e)}")
        app.logger.error(traceback.format_exc())
        return []

async def delete_all_subscriptions():
    global twitch, eventsub
    try:
        if twitch is None:
            app.logger.error("Twitch API not initialized")
            return "Twitch API not initialized"
        subscriptions = await list_all_subscriptions()
        for sub in subscriptions:
            try:
                await twitch.delete_eventsub_subscription(sub.id)
                app.logger.info(f"Deleted subscription: {sub.id}")
            except Exception as e:
                app.logger.error(f"Failed to delete subscription {sub.id}: {str(e)}")
        return "All subscriptions deleted"
    except Exception as e:
        app.logger.error(f"Error deleting subscriptions: {str(e)}")
        app.logger.error(traceback.format_exc())
        return "Error deleting subscriptions"

@celery.task
def add_streamer_task(username, user_id):
    app = create_app()
    
    with app.app_context():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(subscribe_to_events(username, app, user_id))
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
        streamer = Streamer.query.filter_by(twitch_id=int(data['event']['broadcaster_user_id'])).first()
        if streamer:
            streamer.is_live = True
            db.session.commit()
            app.logger.info(f"Streamer {streamer.username} is now live")

            # Log the event
            event = TwitchEvent(event_type='stream_online', event_data=str(data))
            db.session.add(event)
            db.session.commit()

async def on_stream_offline(data: dict):
    app = current_app._get_current_object()
    with app.app_context():
        streamer = Streamer.query.filter_by(twitch_id=int(data['event']['broadcaster_user_id'])).first()
        if streamer:
            streamer.is_live = False
            db.session.commit()
            app.logger.info(f"Streamer {streamer.username} is now offline")

            # Log the event
            event = TwitchEvent(event_type='stream_offline', event_data=str(data))
            db.session.add(event)
            db.session.commit()

async def on_channel_update(data: dict):
    app = current_app._get_current_object()
    with app.app_context():
        streamer = Streamer.query.filter_by(twitch_id=int(data['event']['broadcaster_user_id'])).first()
        if streamer:
            streamer.stream_title = data['event']['title']
            streamer.game_name = data['event']['category_name']
            db.session.commit()
            app.logger.info(f"Channel updated for streamer {streamer.username}")

            # Log the event
            event = TwitchEvent(event_type='channel_update', event_data=str(data))
            db.session.add(event)
            db.session.commit()
