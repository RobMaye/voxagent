import os
import threading
import pyaudio
import queue
import base64
import json
import time
from websocket import create_connection, WebSocketConnectionClosedException
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

# Constants
CHUNK_SIZE = 1024
SAMPLE_RATE = 24000
FORMAT = pyaudio.paInt16
API_KEY = os.getenv("OPENAI_API_KEY")
WS_URL = 'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01'
REENGAGE_DELAY_MS = 500

class VoiceChat:
    def __init__(self):
        self.audio_buffer = bytearray()
        self.mic_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.mic_on_at = 0
        self.mic_active = None
        self.websocket = None
        self.p = pyaudio.PyAudio()
        
    def mic_callback(self, in_data, frame_count, time_info, status):
        if time.time() > self.mic_on_at:
            if self.mic_active != True:
                logging.info('🎙️🟢 Mic active')
                self.mic_active = True
            self.mic_queue.put(in_data)
        else:
            if self.mic_active != False:
                logging.info('🎙️🔴 Mic suppressed')
                self.mic_active = False
        return (None, pyaudio.paContinue)

    def speaker_callback(self, in_data, frame_count, time_info, status):
        bytes_needed = frame_count * 2
        current_buffer_size = len(self.audio_buffer)

        if current_buffer_size >= bytes_needed:
            audio_chunk = bytes(self.audio_buffer[:bytes_needed])
            self.audio_buffer = self.audio_buffer[bytes_needed:]
            self.mic_on_at = time.time() + REENGAGE_DELAY_MS / 1000
        else:
            audio_chunk = bytes(self.audio_buffer) + b'\x00' * (bytes_needed - current_buffer_size)
            self.audio_buffer.clear()

        return (audio_chunk, pyaudio.paContinue)

    def send_mic_audio(self):
        try:
            while not self.stop_event.is_set():
                if not self.mic_queue.empty():
                    mic_chunk = self.mic_queue.get()
                    encoded_chunk = base64.b64encode(mic_chunk).decode('utf-8')
                    message = json.dumps({
                        'type': 'input_audio_buffer.append',
                        'audio': encoded_chunk
                    })
                    try:
                        self.websocket.send(message)
                        logging.info(f'🎤 Sent {len(mic_chunk)} bytes')
                    except WebSocketConnectionClosedException:
                        logging.error('WebSocket connection closed.')
                        break
                    except Exception as e:
                        logging.error(f'Error sending audio: {e}')
                time.sleep(0.01)  # Prevent CPU spinning
        except Exception as e:
            logging.error(f'Send thread error: {e}')

    def receive_audio(self):
        try:
            while not self.stop_event.is_set():
                try:
                    message = self.websocket.recv()
                    if not message:
                        break

                    data = json.loads(message)
                    event_type = data.get('type')
                    
                    if event_type == 'error':
                        logging.error(f"Server error: {data.get('error', {}).get('message', 'Unknown error')}")
                        continue
                        
                    if event_type == 'response.audio.delta':
                        audio_content = base64.b64decode(data['delta'])
                        self.audio_buffer.extend(audio_content)
                        logging.info(f'🔊 Received {len(audio_content)} bytes')
                    elif event_type == 'response.text.delta':
                        logging.info(f'📝 Text: {data.get("delta", "")}')
                    elif event_type == 'response.audio.done':
                        logging.info('🔵 AI finished speaking')

                except WebSocketConnectionClosedException:
                    logging.error('WebSocket connection closed')
                    break
                except json.JSONDecodeError:
                    logging.error('Invalid JSON received')
                except Exception as e:
                    logging.error(f'Receive error: {e}')
        except Exception as e:
            logging.error(f'Receive thread error: {e}')

    def setup_websocket(self):
        try:
            self.websocket = create_connection(
                WS_URL,
                header=[
                    f'Authorization: Bearer {API_KEY}',
                    'OpenAI-Beta: realtime=v1'
                ]
            )
            logging.info('Connected to OpenAI')

            # Send initial configuration
            self.websocket.send(json.dumps({
                'type': 'session.update',
                'input_audio_transcription': True
            }))

            # Create initial response
            self.websocket.send(json.dumps({
                'type': 'response.create',
                'response': {
                    'modalities': ['audio', 'text'],
                    'instructions': '''You are a helpful AI assistant. Your voice and personality should be warm and engaging.
                    Use clear, natural speech and respond concisely. If you need to spell something out, say "spelled" followed by
                    the letters. Try to keep your responses brief but informative.'''
                }
            }))

            return True
        except Exception as e:
            logging.error(f'WebSocket setup failed: {e}')
            return False

    def run(self):
        # Setup audio streams
        mic_stream = self.p.open(
            format=FORMAT,
            channels=1,
            rate=SAMPLE_RATE,
            input=True,
            stream_callback=self.mic_callback,
            frames_per_buffer=CHUNK_SIZE
        )

        speaker_stream = self.p.open(
            format=FORMAT,
            channels=1,
            rate=SAMPLE_RATE,
            output=True,
            stream_callback=self.speaker_callback,
            frames_per_buffer=CHUNK_SIZE
        )

        try:
            mic_stream.start_stream()
            speaker_stream.start_stream()
            logging.info('Audio streams started')

            if not self.setup_websocket():
                return

            # Start communication threads
            send_thread = threading.Thread(target=self.send_mic_audio)
            receive_thread = threading.Thread(target=self.receive_audio)
            
            send_thread.start()
            receive_thread.start()

            logging.info('Voice chat ready! Press Ctrl+C to exit...')
            
            # Keep main thread alive until interrupted
            while True:
                time.sleep(0.1)

        except KeyboardInterrupt:
            logging.info('Shutting down...')
        finally:
            self.stop_event.set()
            
            if self.websocket:
                self.websocket.close()
            
            mic_stream.stop_stream()
            mic_stream.close()
            speaker_stream.stop_stream()
            speaker_stream.close()
            self.p.terminate()
            
            logging.info('Cleanup complete')

if __name__ == "__main__":
    # Clear screen
    os.system('cls' if os.name == 'nt' else 'clear')
    
    print("🎙️ Starting voice chat...")
    print("Requirements:")
    print("1. Make sure you have a working microphone")
    print("2. Make sure you have working speakers/headphones")
    print("3. Required packages: pyaudio, websocket-client")
    print("\nPress Ctrl+C to exit")
    print("\nInitializing...")
    
    chat = VoiceChat()
    chat.run()