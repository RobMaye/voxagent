import asyncio
import discord
from loguru import logger
from typing import Optional, Callable, List
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.frames.frames import Frame, OutputAudioRawFrame
from .client import VoiceRecvClient

class VoiceTransport(FrameProcessor):
    """Transport for handling voice data between Discord and the pipeline."""
    
    def __init__(self, voice_client: VoiceRecvClient, text_channel: discord.TextChannel):
        super().__init__()
        self.voice_client = voice_client
        self.text_channel = text_channel
        self.audio_queue = asyncio.Queue()
        self.output_queue = asyncio.Queue()
        self._first_participant_handlers: List[Callable] = []
        self._audio_callback: Optional[Callable] = None
        self._processing = False
        self._processing_task = None
        logger.info("VoiceTransport initialized")

    async def start(self):
        """Start processing audio data."""
        if not self._processing:
            self._processing = True
            self._processing_task = asyncio.create_task(self._process_audio())
            await self.voice_client.start_receiving()
            logger.info("Voice transport started")

    async def stop(self):
        """Stop processing audio data."""
        self._processing = False
        if self._processing_task:
            self._processing_task.cancel()
            self._processing_task = None
        await self.voice_client.stop_receiving()
        logger.info("Voice transport stopped")

    async def _process_audio(self):
        """Process audio data from the queue."""
        try:
            while self._processing:
                user, audio_data = await self.voice_client.audio_queue.get()
                logger.debug(f"Processing audio from {user.name}")
                # Create frame and push it through the pipeline
                frame = Frame()  # Replace with your actual audio frame type
                frame.data = audio_data
                await self.push_frame(frame)
        except asyncio.CancelledError:
            logger.info("Audio processing cancelled")
        except Exception as e:
            logger.error(f"Error processing audio: {e}")

    def input(self):
        """Get the input processor."""
        return self

    def output(self):
        """Get the output processor."""
        return self

    async def process_frame(self, frame: Frame, direction: str):
        """Process frames through the transport."""
        if isinstance(frame, OutputAudioRawFrame) and self._audio_callback:
            await self._audio_callback(frame.data)
        else:
            await super().process_frame(frame, direction)

    async def notify_first_participant(self, participant: discord.Member):
        """Notify handlers when first participant joins."""
        logger.info(f"First participant notification: {participant.name}")
        for handler in self._first_participant_handlers:
            await handler(self, {"id": participant.id, "name": participant.display_name})

    def event_handler(self, event_name: str):
        """Decorator for registering event handlers."""
        def decorator(func):
            if event_name == "on_first_participant_joined":
                self._first_participant_handlers.append(func)
            return func
        return decorator

    def get_debug_info(self) -> dict:
        """Get debug information about the transport."""
        return {
            "processing": self._processing,
            "has_processing_task": self._processing_task is not None,
            "audio_queue_size": self.audio_queue.qsize(),
            "output_queue_size": self.output_queue.qsize(),
            "has_first_participant_handlers": len(self._first_participant_handlers) > 0,
            "voice_client": self.voice_client.get_debug_info() if self.voice_client else None
        } 