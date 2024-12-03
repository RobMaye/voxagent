from discord.ext import voice_recv
import asyncio
from loguru import logger

class AudioCaptureSink(voice_recv.AudioSink):
    """Audio sink for capturing voice data from Discord."""
    
    def __init__(self, audio_queue: asyncio.Queue, loop=None):
        super().__init__()
        self.audio_queue = audio_queue
        self.loop = loop or asyncio.get_event_loop()
        self._active = True
        logger.info(f"AudioCaptureSink initialized with queue {id(self.audio_queue)}")
        logger.debug(f"AudioCaptureSink created with event loop {id(self.loop)}")

    def write(self, user, data: voice_recv.VoiceData):
        """Handle incoming voice data."""
        try:
            # Log every write call with all available data
            logger.info(f"Write called with user: {user}")
            logger.info(f"Data type: {type(data)}")
            logger.info(f"Data attributes: {dir(data)}")
            logger.info(f"Data dict: {vars(data)}")
            
            if not self._active:
                logger.warning("Write called but sink is not active")
                return

            if hasattr(data, 'opus'):
                logger.info(f"Has opus data: {len(data.opus) if data.opus else 'None'}")
                
            if hasattr(data, 'pcm'):
                logger.info(f"Has PCM data: {len(data.pcm) if data.pcm else 'None'}")
                if data.pcm is not None:
                    try:
                        future = asyncio.run_coroutine_threadsafe(
                            self.audio_queue.put_nowait((user, data.pcm)),
                            self.loop
                        )
                        future.result(timeout=1.0)
                        logger.debug(f"Successfully queued audio data from {user}")
                    except asyncio.QueueFull:
                        logger.warning("Audio queue is full, dropping packet")
                    except Exception as e:
                        logger.error(f"Failed to queue audio data: {e}")
            else:
                logger.warning("No PCM or opus data available")
                
        except Exception as e:
            logger.exception(f"Exception in write method: {e}")

    def cleanup(self):
        """Clean up resources."""
        try:
            logger.info("AudioCaptureSink cleanup triggered")
            self._active = False
            
            try:
                queue_size = self.audio_queue.qsize()
                logger.info(f"Audio queue size at cleanup: {queue_size}")
            except NotImplementedError:
                logger.warning("Queue size check not supported")
            
            logger.debug(f"AudioCaptureSink event loop at cleanup: {id(self.loop)}")
            logger.info("AudioCaptureSink cleanup complete")
        except Exception as e:
            logger.exception(f"Error during cleanup: {e}")
        
    @property
    def wants_opus(self) -> bool:
        """Return False since we want PCM data, not Opus."""
        return False