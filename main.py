# main.py

import os
import sys
import ssl
import certifi
import aiohttp
import discord
from discord.ext import commands
import asyncio
import signal
from loguru import logger
from dotenv import load_dotenv
import json
import logging
import traceback

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_response import (
    LLMAssistantResponseAggregator,
    LLMUserResponseAggregator,
)
from services.openai import BaseOpenAILLMService, OpenAITTSService
from bot import FitnessOnboardingAgent
from voice import VoiceRecvClient, VoiceTransport

# Configure SSL for Discord
ssl_context = ssl.create_default_context()
ssl_context.check_hostname = False
ssl_context.verify_mode = ssl.CERT_NONE

# Load environment variables
load_dotenv(override=True)

# Configure logger with color
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | <level>{message}</level>",
    level="DEBUG",
)
logger.add("logs/voxagent.log", rotation="10 MB")

# Bot configuration
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4")

# Channel names
TEXT_CHANNEL_DISPLAY = "help-desk"
VOICE_CHANNEL_DISPLAY = "drop-in-chat"

# Initialize Discord bot with all required intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True
intents.guilds = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Required voice permissions
VOICE_PERMISSIONS = discord.Permissions(
    connect=True,
    speak=True,
    use_voice_activation=True,
    priority_speaker=True,
    mute_members=True,
    deafen_members=True,
    move_members=True,
    view_channel=True,
    send_messages=True,
    read_messages=True,
    read_message_history=True,
    manage_channels=True
)

voice_clients = {}
pipelines = {}

async def setup_pipeline(guild, voice_client, text_channel):
    """Set up the audio processing pipeline."""
    try:
        transport = VoiceTransport(voice_client, text_channel)
        logger.info("Transport initialized")
        
        # Initialize agent and services
        logger.info("Setting up pipeline components...")
        
        agent = FitnessOnboardingAgent()
        logger.info("Agent initialized")
        
        tts = OpenAITTSService(api_key=OPENAI_API_KEY, voice="shimmer")
        logger.info("TTS service initialized")
        
        messages = agent.messages
        tma_in = LLMUserResponseAggregator(messages)
        tma_out = LLMAssistantResponseAggregator(messages)
        logger.info("Message aggregators initialized")
        
        llm = BaseOpenAILLMService(
            api_key=OPENAI_API_KEY,
            model=OPENAI_MODEL,
            functions=agent.functions,
            function_call="auto",
        )
        logger.info("LLM service initialized")

        # Register functions with their handlers
        for function in agent.functions:
            function_name = function['name']
            handler = getattr(agent, function_name, None)
            if handler:
                llm.register_function(function_name, handler)
                logger.info(f"Registered function: {function_name}")
            else:
                logger.warning(f"No handler found for function '{function_name}'")

        logger.info("Creating pipeline...")
        # Create pipeline
        pipeline = Pipeline(
            [
                transport.input(),
                tma_in,
                llm,
                tts,
                transport.output(),
                tma_out,
            ]
        )
        logger.info("Pipeline created")

        # Create pipeline task with parameters
        task = PipelineTask(pipeline, PipelineParams(allow_interruptions=True))
        logger.info("Pipeline task created")

        # Create and start runner
        runner = PipelineRunner()
        asyncio.create_task(runner.run(task))
        logger.info("Pipeline runner started")
        
        # Start the transport
        await transport.start()
        logger.info("Transport started")
        
        # Set up first participant handler
        @transport.event_handler("on_first_participant_joined")
        async def on_first_participant_joined(transport, participant):
            logger.info(f"Participant {participant['name']} joined the voice chat.")
            # Start conversation by queuing the initial system message
            await task.queue_frames([LLMMessagesFrame([messages[0]])])
        
        # Store pipeline components
        pipelines[guild.id] = (transport, task)
        logger.info("Pipeline components stored")

        return transport
    except Exception as e:
        logger.exception(f"Error in setup_pipeline: {e}")
        raise

async def setup_voice_connection(guild, voice_channel, text_channel):
    """Set up voice connection in the specified guild and channel."""
    try:
        logger.debug(f"Voice channel details: id={voice_channel.id}, name={voice_channel.name}, type={type(voice_channel)}")
        
        # Check bot permissions
        permissions = voice_channel.permissions_for(guild.me)
        logger.debug(f"Bot permissions in voice channel: {permissions}")
        
        # Connect using our VoiceRecvClient
        voice_client = await voice_channel.connect(cls=VoiceRecvClient, timeout=60.0, reconnect=True)
        logger.debug(f"Voice client created: {voice_client}")
        
        # Wait for the voice client to be ready
        tries = 0
        while not voice_client.is_connected() and tries < 5:
            logger.debug("Waiting for voice client to connect...")
            await asyncio.sleep(1)
            tries += 1

        if voice_client.is_connected():
            logger.info("Voice client connected")
            
            # Set up the pipeline
            try:
                transport = await setup_pipeline(guild, voice_client, text_channel)
                logger.info("Pipeline setup complete")
                
                # Store voice client for later use
                voice_clients[guild.id] = voice_client
                
                # Add a debug message
                await text_channel.send("🎤 Voice system initialized. Try speaking!")
                
                return voice_client
            except Exception as e:
                logger.error(f"Failed to set up pipeline: {e}")
                await voice_client.disconnect()
                raise
        else:
            logger.error("Voice client failed to connect")
            return None

    except Exception as e:
        logger.error(f"Failed to set up voice connection in {guild.name}")
        logger.error(traceback.format_exc())
        return None

@bot.command(name='debug')
async def debug_command(ctx):
    """Debug command to check pipeline state."""
    try:
        guild = ctx.guild
        if guild.id in pipelines:
            transport, task = pipelines[guild.id]
            debug_info = transport.get_debug_info()
            await ctx.send(f"Debug Info:\n```json\n{json.dumps(debug_info, indent=2)}\n```")
        else:
            await ctx.send("❌ No pipeline found for this guild")
    except Exception as e:
        logger.error(f"Error in debug command: {e}")
        await ctx.send(f"❌ Error: {str(e)}")

@bot.command(name='restart')
async def restart_pipeline(ctx):
    """Restart the pipeline."""
    try:
        guild = ctx.guild
        if guild.id in pipelines:
            # Get the current voice and text channels
            voice_channel = ctx.guild.voice_client.channel if ctx.guild.voice_client else None
            text_channel = ctx.channel
            
            # Clean up existing pipeline
            transport, task = pipelines[guild.id]
            await transport.stop()
            
            if ctx.guild.voice_client:
                await ctx.guild.voice_client.disconnect()
            
            await asyncio.sleep(1)  # Give it a moment to clean up
            
            # Reconnect and set up new pipeline
            if voice_channel:
                voice_client = await setup_voice_connection(guild, voice_channel, text_channel)
                if voice_client:
                    await ctx.send("✅ Pipeline restarted successfully")
                else:
                    await ctx.send("❌ Failed to restart pipeline")
            else:
                await ctx.send("❌ No voice channel found")
        else:
            await ctx.send("❌ No pipeline found for this guild")
    except Exception as e:
        logger.error(f"Error in restart command: {e}")
        await ctx.send(f"❌ Error restarting pipeline: {str(e)}")

@bot.event
async def on_ready():
    """Event handler for when the bot is ready."""
    logger.info(f"{bot.user} has connected to Discord!")
    logger.info(f'Bot is in {len(bot.guilds)} guilds')
    
    # Load voice commands
    try:
        await bot.load_extension('voice.commands')
        logger.info("Voice commands loaded")
    except Exception as e:
        logger.error(f"Failed to load voice commands: {e}")
    
    # Set up channels and join voice in all guilds
    for guild in bot.guilds:
        logger.debug(f"Setting up guild: {guild.name}")
        text_channel, voice_channel = await ensure_channels(guild)
        if text_channel and voice_channel:
            logger.debug(f"Found channels: text={text_channel.name}, voice={voice_channel.name}")
            voice_client = await setup_voice_connection(guild, voice_channel, text_channel)
            if voice_client:
                logger.info(f"Successfully set up voice in {guild.name}")
            else:
                logger.error(f"Failed to set up voice in {guild.name}")
        else:
            logger.warning(f"Could not find required channels in {guild.name}")
    
    # Generate invite link with required permissions
    app_info = await bot.application_info()
    invite_url = discord.utils.oauth_url(
        app_info.id,
        permissions=VOICE_PERMISSIONS
    )
    logger.info(f'Invite URL: {invite_url}')

async def ensure_channels(guild):
    """Ensure the required channels exist in the guild."""
    # Check/create text channel
    text_channel = discord.utils.get(guild.text_channels, name=TEXT_CHANNEL_DISPLAY)
    if not text_channel:
        try:
            text_channel = await guild.create_text_channel(TEXT_CHANNEL_DISPLAY)
            logger.info(f"Created text channel: {TEXT_CHANNEL_DISPLAY}")
        except discord.errors.Forbidden:
            logger.error(f"Cannot create text channel in {guild.name}")
            return None, None

    # Check/create voice channel
    voice_channel = discord.utils.get(guild.voice_channels, name=VOICE_CHANNEL_DISPLAY)
    if not voice_channel:
        try:
            voice_channel = await guild.create_voice_channel(VOICE_CHANNEL_DISPLAY)
            logger.info(f"Created voice channel: {VOICE_CHANNEL_DISPLAY}")
        except discord.errors.Forbidden:
            logger.error(f"Cannot create voice channel in {guild.name}")
            return text_channel, None

    return text_channel, voice_channel

def terminate_process():
    """Terminate the process."""
    logger.info("Terminating process...")
    os.kill(os.getpid(), signal.SIGKILL)

async def cleanup():
    """Cleanup function to disconnect voice clients and stop pipelines."""
    logger.info("Starting cleanup...")
    
    try:
        # Stop all transports
        for guild_id, (transport, task) in pipelines.items():
            try:
                await transport.stop()
            except:
                pass
        
        # Cancel all tasks
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        
        # Close bot
        if not bot.is_closed():
            await bot.close()
            
    except Exception as e:
        logger.error(f"Error during cleanup: {e}")
    finally:
        terminate_process()

def signal_handler(signum, frame):
    """Handle interrupt signal."""
    logger.info("Received interrupt signal, terminating...")
    terminate_process()

if __name__ == "__main__":
    # Check opus
    if not discord.opus.is_loaded():
        logger.warning("Opus not loaded - attempting to load...")
        try:
            # Try different opus library paths
            opus_paths = [
                '/opt/homebrew/lib/libopus.dylib',  # Homebrew path
                'libopus',  # System path
                'opus'  # Default path
            ]
            
            for path in opus_paths:
                try:
                    discord.opus.load_opus(path)
                    logger.info(f"Opus loaded successfully from {path}")
                    break
                except Exception as e:
                    logger.debug(f"Failed to load opus from {path}: {e}")
            else:
                raise Exception("Could not load opus from any path")
        except Exception as e:
            logger.error(f"Failed to load opus: {e}")
            sys.exit(1)
    
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        # Create new event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        # Start the bot
        loop.run_until_complete(bot.start(DISCORD_TOKEN))
        loop.run_forever()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received...")
    except Exception as e:
        logger.error(f"Error in main: {e}")
    finally:
        try:
            loop.run_until_complete(cleanup())
        except:
            terminate_process()
