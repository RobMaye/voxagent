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
import io
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
from frames import ActionFrame
from pipecat.frames.frames import OutputAudioRawFrame, LLMMessagesFrame
from pipecat.processors.frame_processor import FrameProcessor

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
intents.voice_states = True  # Required for voice state updates
intents.guilds = True
bot = commands.Bot(
    command_prefix='!',
    intents=intents
)

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

# Store voice clients and pipelines for each guild
voice_clients = {}
pipelines = {}

class DiscordTransport(FrameProcessor):
    def __init__(self, voice_client, text_channel):
        super().__init__()
        self.voice_client = voice_client
        self.text_channel = text_channel
        self.audio_queue = asyncio.Queue()
        self.output_queue = asyncio.Queue()
        self._first_participant_handlers = []
        self._audio_callback = None

    def set_audio_callback(self, callback):
        self._audio_callback = callback

    def input(self):
        return QueueFrameProcessor(self.audio_queue)

    def output(self):
        return QueueFrameProcessor(self.output_queue)

    async def on_voice_packet(self, audio_data):
        """Handle incoming voice packets."""
        await self.audio_queue.put(audio_data)

    async def process_frame(self, frame, direction):
        """Process frames, handling audio output."""
        if isinstance(frame, OutputAudioRawFrame) and self._audio_callback:
            await self._audio_callback(frame.data)
        else:
            await super().process_frame(frame, direction)

    async def notify_first_participant(self, participant):
        """Notify handlers when first participant joins."""
        for handler in self._first_participant_handlers:
            await handler(self, {"id": participant.id, "name": participant.display_name})

    def event_handler(self, event_name):
        def decorator(func):
            if event_name == "on_first_participant_joined":
                self._first_participant_handlers.append(func)
            return func
        return decorator

class QueueFrameProcessor(FrameProcessor):
    def __init__(self, queue):
        super().__init__()
        self.queue = queue

    async def process_frame(self, frame, direction):
        await self.queue.put(frame)
        await self.push_frame(frame, direction)

class VoiceState:
    def __init__(self):
        self.speaking = {}

voice_states = VoiceState()

async def setup_pipeline(guild, voice_client, text_channel):
    """Set up the audio processing pipeline."""
    try:
        transport = DiscordTransport(voice_client, text_channel)
        
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
        
        # Initialize OpenAI client with minimal settings
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

        # Create and store pipeline task
        task = PipelineTask(pipeline, PipelineParams(allow_interruptions=True))
        pipelines[guild.id] = (transport, task)
        logger.info("Pipeline task created")

        # Set up first participant handler
        @transport.event_handler("on_first_participant_joined")
        async def on_first_participant_joined(transport, participant):
            logger.info(f"Participant {participant['name']} joined the voice chat.")
            # Start conversation by queuing the initial system message
            await task.queue_frames([LLMMessagesFrame([messages[0]])])

        # Start pipeline runner
        runner = PipelineRunner()
        asyncio.create_task(runner.run(task))
        logger.info("Pipeline runner started")

        return transport
    except Exception as e:
        logger.exception(f"Error in setup_pipeline: {e}")
        raise

class VoiceRecorder:
    def __init__(self, transport):
        self.transport = transport
        self.recording = {}
        logger.info("Voice recorder initialized")

    async def on_voice_state_update(self, member, before, after):
        """Handle voice state updates."""
        if member.bot:
            return

        if after and after.channel and after.channel.name == VOICE_CHANNEL_DISPLAY:
            logger.info(f"Started recording {member.name}")
            self.recording[member.id] = True
        elif before and before.channel and before.channel.name == VOICE_CHANNEL_DISPLAY:
            logger.info(f"Stopped recording {member.name}")
            self.recording[member.id] = False

    async def on_voice_packet(self, member_id, packet):
        """Handle incoming voice packets."""
        if member_id in self.recording and self.recording[member_id]:
            logger.info(f"Received voice packet from user {member_id}")
            await self.transport.on_voice_packet(packet)

async def setup_voice_connection(guild, voice_channel, text_channel):
    """Set up voice connection in the specified guild and channel."""
    try:
        logger.debug(f"Voice channel details: id={voice_channel.id}, name={voice_channel.name}, type={type(voice_channel)}")
        
        # Check bot permissions
        permissions = voice_channel.permissions_for(guild.me)
        logger.debug(f"Bot permissions in voice channel: {permissions}")
        
        # Connect to voice channel using default VoiceClient
        voice_client = await voice_channel.connect(timeout=60.0, reconnect=True)
        logger.debug(f"Voice client created: {voice_client}")
        logger.debug(f"Voice client state: connected={voice_client.is_connected()}, playing={voice_client.is_playing()}")
        
        if voice_client.is_connected():
            logger.info("Voice client connected")
            # Play test sound
            await play_sound(voice_client, "assets/ding.wav")
            return voice_client
        else:
            logger.error("Voice client failed to connect")
            return None

    except Exception as e:
        logger.error(f"Failed to set up voice connection in {guild.name}")
        logger.error(traceback.format_exc())
        return None

async def play_sound(voice_client, sound_file):
    """Play a sound file through the voice client."""
    try:
        if not isinstance(voice_client, discord.VoiceClient):
            logger.error(f"Invalid voice client type: {type(voice_client)}")
            return

        if voice_client.is_playing():
            voice_client.stop()
        
        # Create audio source with specific options for better compatibility
        try:
            options = '-loglevel warning -ar 48000 -ac 2 -f s16le -acodec pcm_s16le'
            source = discord.FFmpegPCMAudio(
                sound_file,
                before_options='-nostdin',  # Disable stdin to prevent ffmpeg from hanging
                options=options  # Use specific format options
            )
            logger.debug(f"Created audio source for {sound_file}")
        except Exception as e:
            logger.error(f"Failed to create audio source: {e}")
            return

        # Play the audio
        def after_callback(error):
            if error:
                logger.error(f"Error in playback: {error}")
            else:
                logger.debug("Finished playing sound")

        voice_client.play(source, after=after_callback)
        logger.info(f"Playing sound: {sound_file}")
    except Exception as e:
        logger.error(f"Error playing sound: {e}")

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

@bot.event
async def on_ready():
    """Event handler for when the bot is ready."""
    logger.info(f'{bot.user} has connected to Discord!')
    logger.info(f'Bot is in {len(bot.guilds)} guilds')
    
    # Set up channels and join voice in all guilds
    for guild in bot.guilds:
        logger.debug(f"Setting up guild: {guild.name}")
        text_channel, voice_channel = await ensure_channels(guild)
        if text_channel and voice_channel:
            logger.debug(f"Found channels: text={text_channel.name}, voice={voice_channel.name}")
            await setup_voice_connection(guild, voice_channel, text_channel)
        else:
            logger.warning(f"Could not find required channels in {guild.name}")
    
    # Generate invite link with required permissions
    app_info = await bot.application_info()
    invite_url = discord.utils.oauth_url(
        app_info.id,
        permissions=VOICE_PERMISSIONS
    )
    logger.info(f'Invite URL: {invite_url}')

@bot.event
async def on_voice_state_update(member, before, after):
    """Handle voice state updates."""
    if member.id != bot.user.id:  # Don't respond to bot's own state changes
        if before.channel != after.channel:
            if after.channel and after.channel.guild.voice_client:
                # Member joined a channel where bot is present
                logger.info(f"User {member.name} joined voice channel")
                await play_sound(after.channel.guild.voice_client, "assets/ding.wav")

    # Debug voice state
    logger.debug(f"Voice state update for {member.name}:")
    if before.channel:
        logger.debug(f"  Before: channel={before.channel.name}, self_mute={before.self_mute}, self_deaf={before.self_deaf}")
    else:
        logger.debug(f"  Before: channel=None, self_mute={before.self_mute}, self_deaf={before.self_deaf}")
    
    if after.channel:
        logger.debug(f"  After: channel={after.channel.name}, self_mute={after.self_mute}, self_deaf={after.self_deaf}")
        logger.debug(f"User {member.name} joined voice channel {after.channel.name}")
    else:
        logger.debug(f"  After: channel=None, self_mute={after.self_mute}, self_deaf={after.self_deaf}")

    if member.id == bot.user.id:
        logger.debug("Voice state update is for bot")
        return

    # If someone joins our voice channel
    if after and after.channel and after.channel.name == VOICE_CHANNEL_DISPLAY:
        guild = member.guild
        logger.debug(f"User {member.name} joined voice channel {after.channel.name}")
        
        if guild.id in pipelines:
            transport, _ = pipelines[guild.id]
            await transport.notify_first_participant(member)
            
            # Update recorder state
            voice_client = voice_clients.get(guild.id)
            logger.debug(f"Voice client for guild: {voice_client}")
            if voice_client and hasattr(voice_client, 'recorder'):
                logger.debug("Updating recorder state")
                await voice_client.recorder.on_voice_state_update(member, before, after)
            else:
                logger.warning("No voice client or recorder found")
            
            # Send welcome message
            text_channel = discord.utils.get(guild.text_channels, name=TEXT_CHANNEL_DISPLAY)
            if text_channel:
                await text_channel.send(f"👋 {member.name} has joined the voice chat!")

    # If someone leaves our voice channel
    elif before and before.channel and before.channel.name == VOICE_CHANNEL_DISPLAY:
        logger.debug(f"User {member.name} left voice channel {before.channel.name}")
        # Update recorder state
        guild = member.guild
        voice_client = voice_clients.get(guild.id)
        if voice_client and hasattr(voice_client, 'recorder'):
            await voice_client.recorder.on_voice_state_update(member, before, after)
            
        text_channel = discord.utils.get(member.guild.text_channels, name=TEXT_CHANNEL_DISPLAY)
        if text_channel:
            await text_channel.send(f"👋 {member.name} has left the voice chat!")

@bot.event
async def on_voice_receive(user, data):
    """Handle received voice data."""
    if user.bot:
        return
        
    guild = user.guild
    voice_client = voice_clients.get(guild.id)
    if voice_client and hasattr(voice_client, 'recorder'):
        await voice_client.recorder.on_voice_packet(user.id, data)

def terminate_process():
    """Force terminate the process."""
    logger.warning("Force terminating process...")
    os.kill(os.getpid(), signal.SIGKILL)

async def cleanup():
    """Cleanup function to disconnect voice clients and stop pipelines."""
    logger.info("Starting cleanup...")
    
    try:
        # Cancel all tasks
        tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        
        # Disconnect voice clients
        for voice_client in voice_clients.values():
            try:
                if voice_client and voice_client.is_connected():
                    await voice_client.disconnect(force=True)
            except:
                pass
        
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
