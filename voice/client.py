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
from dataclasses import dataclass
from typing import Optional

@dataclass
class AudioPacket:
    """Container for audio data with sequence information."""
    data: bytes
    sequence: int
    timestamp: float

class OpusBufferedAudio(discord.AudioSource):
    """Buffered audio source that properly handles Discord's audio requirements.
    
    Input format (from discord-voice-recv):
    - 48kHz sample rate
    - 2 channels (stereo)
    - 16-bit signed little-endian PCM
    
    Output format (for discord.py):
    - 48kHz sample rate
    - 2 channels (stereo)
    - 16-bit signed little-endian PCM
    """
    
    def __init__(self, buffer_duration: float = 0.1):
        self.lock = Lock()
        self.frame_duration = 0.02  # 20ms frames
        self.sample_rate = 48000
        self.channels = 2  # Both input and output are stereo
        self.bytes_per_sample = 2  # 16-bit audio
        self.buffer_size = int(buffer_duration * 1000)  # Buffer size in ms
        
        # Calculate frame size (same for input and output since format matches)
        self.frame_size = int(self.sample_rate * self.frame_duration * self.bytes_per_sample * self.channels)
        
        # Sequence tracking
        self.next_sequence = 0
        self.last_read_sequence = -1
        self.last_timestamp = 0.0
        
        # Ordered buffer with max length
        max_frames = int(self.buffer_size / (self.frame_duration * 1000))
        self.buffer = deque(maxlen=max_frames)
        
        logger.info(f"Audio configuration:")
        logger.info(f"- Sample rate: {self.sample_rate}Hz")
        logger.info(f"- Channels: {self.channels}")
        logger.info(f"- Frame size: {self.frame_size} bytes ({self.frame_duration * 1000}ms)")
        logger.info(f"- Buffer size: {self.buffer_size}ms ({max_frames} frames)")
        
        # Start FFmpeg process
        self._start_ffmpeg()
        
        self.running = True
        self.buffer_thread = threading.Thread(target=self._process_buffer, daemon=True)
        self.buffer_thread.start()

    def _start_ffmpeg(self):
        """Start FFmpeg process with exact Discord specifications."""
        if hasattr(self, 'process') and self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=1.0)
            except Exception as e:
                logger.error(f"Error cleaning up old FFmpeg process: {e}")

        # FFmpeg command to pass through audio unchanged
        self.process = subprocess.Popen(
            [
                'ffmpeg',
                # Input settings
                '-f', 's16le',           # Format: Signed 16-bit little-endian
                '-ar', str(self.sample_rate),  # Sample rate: 48kHz
                '-ac', str(self.channels),     # Channels: 2 (stereo)
                '-i', 'pipe:0',          # Read from stdin
                
                # Output settings (same as input)
                '-f', 's16le',           # Format: Signed 16-bit little-endian
                '-ar', str(self.sample_rate),  # Sample rate: 48kHz
                '-ac', str(self.channels),     # Channels: 2 (stereo)
                
                # No audio filters needed since format matches
                '-c:a', 'pcm_s16le',     # Explicit codec
                
                '-loglevel', 'warning',
                'pipe:1'                 # Write to stdout
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        logger.info("FFmpeg process started with Discord audio specifications")
    
    def _process_buffer(self):
        """Process the buffer in a separate thread."""
        while self.running:
            with self.lock:
                if self.buffer:
                    # Get the next packet in sequence
                    next_packet = None
                    for packet in sorted(self.buffer, key=lambda x: x.sequence):
                        if packet.sequence == self.last_read_sequence + 1:
                            next_packet = packet
                            self.buffer.remove(packet)
                            break
                    
                    if next_packet:
                        try:
                            if not hasattr(self, '_logged_first_packet'):
                                logger.info(f"Processing first audio packet: sequence {next_packet.sequence}")
                                self._logged_first_packet = True
                            
                            # Maintain timing between packets
                            time_since_last = time.time() - self.last_timestamp
                            if time_since_last < self.frame_duration:
                                time.sleep(self.frame_duration - time_since_last)
                            
                            self.process.stdin.write(next_packet.data)
                            self.process.stdin.flush()
                            self.last_read_sequence = next_packet.sequence
                            self.last_timestamp = time.time()
                            
                        except Exception as e:
                            logger.error(f"Error writing to FFmpeg: {e}")
                            if self.process and self.process.stderr:
                                error = self.process.stderr.read()
                                if error:
                                    logger.error(f"FFmpeg error: {error.decode()}")
                            self._start_ffmpeg()
            time.sleep(0.001)
    
    def add_audio(self, pcm_data: bytes):
        """Add PCM data to the buffer with sequence number."""
        with self.lock:
            # Create packet with sequence number and timestamp
            packet = AudioPacket(
                data=pcm_data,
                sequence=self.next_sequence,
                timestamp=time.time()
            )
            self.next_sequence += 1
            
            # Log buffer stats periodically
            if not hasattr(self, '_last_log') or time.time() - self._last_log > 5.0:
                logger.debug(f"Buffer status: {len(self.buffer)}/{self.buffer.maxlen} packets, "
                           f"next_seq={self.next_sequence}, last_read={self.last_read_sequence}")
                self._last_log = time.time()
            
            self.buffer.append(packet)
    
    def read(self) -> bytes:
        """Read audio data in 20ms chunks."""
        try:
            data = self.process.stdout.read(self.frame_size)
            if not data:
                return b'\x00' * self.frame_size
            
            if len(data) != self.frame_size:
                logger.warning(f"Unexpected frame size: got {len(data)}, expected {self.frame_size}")
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
                if not self.is_playing():
                    logger.debug("Starting audio playback")
                    self.play(self.audio_source, after=lambda e: logger.error(f"Player error: {e}") if e else None)
            except Exception as e:
                logger.error(f"Error processing audio packet: {e}")

    async def update_audio_settings(self, **kwargs):
        """Update audio settings."""
        if self.audio_source:
            self.audio_source.update_settings(**kwargs)
            return self.audio_source.get_settings()
        return None

    def get_audio_settings(self):
        """Get current audio settings."""
        if self.audio_source:
            return self.audio_source.get_settings()
        return None

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
            self.audio_source = OpusBufferedAudio(buffer_duration=0.1)  # 100ms buffer
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