import asyncio
import traceback
from flask import current_app
from twitchAPI.twitch import Twitch
from twitchAPI.eventsub.webhook import EventSubWebhook
from contextlib import asynccontextmanager
import redis
from redis.exceptions import LockError

class TwitchAPI:
    def __init__(self):
        self.twitch = None
        self.eventsub = None

@asynccontextmanager
async def manage_async_loop():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        loop.close()

async def setup_twitch(app):
    app.logger.info("Entering setup_twitch")
    try:
        print_config(app)
        client_id = app.config['TWITCH_CLIENT_ID']
        client_secret = app.config['TWITCH_CLIENT_SECRET']
        app.logger.info(f"Initializing Twitch API with CLIENT_ID: {client_id}")
        twitch = await Twitch(client_id, client_secret)
        await twitch.authenticate_app([])
        app.logger.info("Twitch API authenticated successfully")
        return twitch
    except Exception as e:
        app.logger.error(f"Failed to initialize Twitch API: {str(e)}")
        app.logger.error(traceback.format_exc())
        return None

async def setup_eventsub(app, twitch_instance):
    app.logger.info("Entering setup_eventsub")
    try:
        if not twitch_instance:
            app.logger.error("Twitch instance is None, cannot setup EventSub")
            return None

        callback_url = app.config['CALLBACK_URL']
        port = int(app.config['EVENTSUB_WEBHOOK_PORT'])
        secret = app.config['TWITCH_WEBHOOK_SECRET']
        
        app.logger.info(f"Full callback URL: {callback_url}")
        app.logger.info(f"Initializing EventSubWebhook with CALLBACK_URL: {callback_url}, PORT: {port}")
        eventsub = EventSubWebhook(callback_url, port, secret, twitch_instance)
        
        try:
            if asyncio.iscoroutinefunction(eventsub.start):
                await eventsub.start()
            else:
                eventsub.start()
            app.logger.info("EventSubWebhook started successfully")
        except OSError as e:
            if e.errno == 98:  # Address already in use
                app.logger.info("EventSubWebhook already running")
            else:
                raise
        
        return eventsub
    except Exception as e:
        app.logger.error(f"Failed to setup EventSub: {str(e)}")
        app.logger.error(traceback.format_exc())
        return None

def init_twitch_api(app):
    twitch_api = TwitchAPI()
    
    if twitch_api.twitch is None:
        app.logger.info("Initializing Twitch API")
        
        if not all([app.config.get(key) for key in ['TWITCH_CLIENT_ID', 'TWITCH_CLIENT_SECRET', 'TWITCH_WEBHOOK_SECRET', 'CALLBACK_URL', 'EVENTSUB_WEBHOOK_PORT']]):
            app.logger.error("Missing required configuration values")
            return None

        async def initialize():
            try:
                twitch_api.twitch = await setup_twitch(app)
            except Exception as e:
                app.logger.error(f"Error during initialization: {str(e)}")
                app.logger.error(traceback.format_exc())

        asyncio.run(initialize())
    else:
        app.logger.info("Twitch API already initialized")
    
    return twitch_api.twitch

def print_config(app):
    app.logger.info("Printing configuration values:")
    config_keys = [
        'TWITCH_CLIENT_ID', 'TWITCH_CLIENT_SECRET', 'TWITCH_WEBHOOK_SECRET',
        'BASE_URL', 'CALLBACK_URL', 'EVENTSUB_WEBHOOK_PORT'
    ]
    for key in config_keys:
        value = app.config.get(key, 'Not Set')
        app.logger.info(f"{key}: {'Set' if value != 'Not Set' else value}")
