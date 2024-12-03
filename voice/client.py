from discord.ext import voice_recv
import discord
import asyncio
from loguru import logger
import numpy as np
from collections import deque
from threading import Lock
import subprocess
import threading
import time

class OpusBufferedAudio(discord.AudioSource):
    """Buffered audio source that properly handles Discord's audio requirements."""
    
    def __init__(self, buffer_duration: float = 0.1):
        self.buffer = deque()
        self.lock = Lock()
        self.frame_duration = 0.02  # 20ms frames
        self.input_sample_rate = 48000  # Discord voice receive sample rate
        self.output_sample_rate = 48000  # Discord playback sample rate
        self.input_channels = 1  # Mono input
        self.output_channels = 2  # Stereo output
        self.bytes_per_sample = 2  # 16-bit audio
        
        self.frame_size = int(self.output_sample_rate * self.frame_duration * self.bytes_per_sample * self.output_channels)
        
        logger.info(f"Audio configuration:")
        logger.info(f"- Input: {self.input_sample_rate}Hz, {self.input_channels} channel(s)")
        logger.info(f"- Output: {self.output_sample_rate}Hz, {self.output_channels} channel(s)")
        logger.info(f"- Frame size: {self.frame_size} bytes ({self.frame_duration * 1000}ms)")
        
        self._start_ffmpeg()
        
        self.running = True
        self.buffer_thread = threading.Thread(target=self._process_buffer, daemon=True)
        self.buffer_thread.start()

    def _start_ffmpeg(self):
        """Start the FFmpeg process with correct parameters."""
        if hasattr(self, 'process') and self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=1.0)
            except Exception as e:
                logger.error(f"Error cleaning up old FFmpeg process: {e}")

        # Simple and direct FFmpeg pipeline:
        # 1. Read 48kHz mono PCM
        # 2. Convert to stereo by duplicating the mono channel
        # 3. Output 48kHz stereo PCM
        self.process = subprocess.Popen(
            [
                'ffmpeg',
                '-f', 's16le',           # Input format: Signed 16-bit little-endian
                '-ar', str(self.input_sample_rate),  # Input sample rate
                '-ac', str(self.input_channels),     # Input channels
                '-i', 'pipe:0',          # Read from stdin
                
                # Simple filter to duplicate mono to both stereo channels
                '-af', 'pan=stereo|c0=c0|c1=c0',
                
                '-f', 's16le',           # Output format: Signed 16-bit little-endian
                '-ar', str(self.output_sample_rate), # Output sample rate
                '-ac', str(self.output_channels),    # Output channels
                '-loglevel', 'warning',   # Only show warnings and errors
                'pipe:1'                 # Write to stdout
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        logger.info("FFmpeg process started")
    
    def _process_buffer(self):
        """Process the buffer in a separate thread."""
        while self.running:
            with self.lock:
                if self.buffer:
                    data = self.buffer.popleft()
                    try:
                        if not hasattr(self, '_logged_first_packet'):
                            logger.info(f"Processing first audio packet: {len(data)} bytes")
                            self._logged_first_packet = True
                        
                        self.process.stdin.write(data)
                        self.process.stdin.flush()
                    except Exception as e:
                        logger.error(f"Error writing to FFmpeg: {e}")
                        if self.process and self.process.stderr:
                            error = self.process.stderr.read()
                            if error:
                                logger.error(f"FFmpeg error: {error.decode()}")
                        self._start_ffmpeg()  # Restart FFmpeg
            time.sleep(0.001)  # Small sleep to prevent CPU spinning
    
    def add_audio(self, pcm_data: bytes):
        """Add PCM data to the buffer."""
        with self.lock:
            if not hasattr(self, '_last_log') or time.time() - self._last_log > 5.0:
                logger.debug(f"Adding audio packet: {len(pcm_data)} bytes")
                self._last_log = time.time()
            self.buffer.append(pcm_data)
    
    def read(self) -> bytes:
        """Read audio data in 20ms chunks."""
        try:
            # Read exactly one frame of audio
            data = self.process.stdout.read(self.frame_size)
            if not data:
                return b'\x00' * self.frame_size  # Return silence if no data
            
            if len(data) != self.frame_size:
                logger.warning(f"Unexpected frame size: got {len(data)}, expected {self.frame_size}")
                # Pad with silence if we got less data than expected
                data = data.ljust(self.frame_size, b'\x00')
            
            return data
            
        except Exception as e:
            logger.error(f"Error reading audio: {e}")
            if self.process and self.process.stderr:
                error = self.process.stderr.read()
                if error:
                    logger.error(f"FFmpeg error: {error.decode()}")
            return b'\x00' * self.frame_size
    
    def cleanup(self):
        """Clean up resources."""
        self.running = False
        if self.buffer_thread.is_alive():
            self.buffer_thread.join(timeout=1.0)
        if self.process:
            try:
                self.process.stdin.close()
                self.process.stdout.close()
                self.process.terminate()
                self.process.wait(timeout=1.0)
            except Exception as e:
                logger.error(f"Error cleaning up FFmpeg process: {e}")

    def is_opus(self):
        """This source provides PCM audio."""
        return False

class VoiceRecvClient(voice_recv.VoiceRecvClient):
    """Extended voice client with reception capabilities."""
    
    def __init__(self, client: discord.Client, channel: discord.VoiceChannel):
        super().__init__(client, channel)
        self._sink = None
        self.audio_source = None
        self.audio_queue = asyncio.Queue()
        logger.info("VoiceRecvClient initialized")

    def audio_callback(self, user, data: voice_recv.VoiceData):
        """Handle incoming audio data."""
        if data.pcm and self.audio_source:
            try:
                self.audio_source.add_audio(data.pcm)
                # Start playing if not already playing
                if not self.is_playing():
                    logger.debug("Starting audio playback")
                    self.play(self.audio_source, after=lambda e: logger.error(f"Player error: {e}") if e else None)
            except Exception as e:
                logger.error(f"Error processing audio packet: {e}")

    async def wait_for_connection(self, timeout: float = 5.0) -> bool:
        """Wait for voice connection to be established."""
        start_time = asyncio.get_event_loop().time()
        while not self.is_connected():
            if asyncio.get_event_loop().time() - start_time > timeout:
                logger.warning("Timed out waiting for voice connection")
                return False
            await asyncio.sleep(0.1)
        logger.info("Voice connection established")
        return True

    async def start_receiving(self):
        """Start receiving voice data."""
        if not self._sink:
            if not await self.wait_for_connection():
                logger.error("Could not start receiving - connection timeout")
                return

            logger.info("Creating new audio source and sink")
            self.audio_source = OpusBufferedAudio()
            self._sink = voice_recv.BasicSink(self.audio_callback)
            try:
                logger.info("Starting to listen")
                self.listen(self._sink)
                logger.info("Successfully started listening")
            except Exception as e:
                logger.error(f"Failed to start listening: {e}")
                self._sink = None
                if self.audio_source:
                    self.audio_source.cleanup()
                self.audio_source = None
                raise

    async def stop_receiving(self):
        """Stop receiving voice data."""
        if self._sink:
            logger.info("Stopping voice reception")
            self.stop_listening()
            if self.is_playing():
                self.stop()
            if self.audio_source:
                self.audio_source.cleanup()
            self._sink = None
            self.audio_source = None

    def is_receiving(self) -> bool:
        """Check if currently receiving voice data."""
        return self._sink is not None and self.is_connected()

    async def on_voice_state_update(self, data: dict):
        """Handle voice state updates."""
        try:
            logger.debug(f"Voice state update received: {data}")
            await super().on_voice_state_update(data)
            
            if 'user_id' in data:
                user_id = int(data['user_id'])
                member = self.guild.get_member(user_id)
                if member:
                    in_channel = data.get('channel_id') == str(self.channel.id)
                    if in_channel:
                        logger.info(f"User {member.name} joined voice channel")
                        if not self.is_receiving():
                            await asyncio.sleep(1)
                            await self.start_receiving()
                    else:
                        logger.info(f"User {member.name} left voice channel")
                        if not any(u.id != self.user.id for u in self.channel.members):
                            logger.info("No users left in channel, stopping reception")
                            await self.stop_receiving()
        except Exception as e:
            logger.exception(f"Error in voice state update: {e}")

    async def received_packet(self, packet):
        """Handle raw voice packets."""
        logger.debug(f"Received raw voice packet")
        await super().received_packet(packet) 