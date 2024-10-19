import asyncio
import os
import sys

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_response import (
    LLMAssistantResponseAggregator,
    LLMUserResponseAggregator,
)
from services.openai import CustomOpenAILLMService, OpenAITTSService
from pipecat.transports.services.daily import DailyParams, DailyTransport
from pipecat.frames.frames import LLMMessagesFrame  # Import LLMMessagesFrame
from bot import FitnessOnboardingAgent

from loguru import logger
from dotenv import load_dotenv

import wave
from frames import ActionFrame
from pipecat.frames.frames import OutputAudioRawFrame
from pipecat.processors.frame_processor import FrameProcessor

script_dir = os.path.dirname(os.path.abspath(__file__))
ding_wav_path = os.path.join(script_dir, "assets", "ding.wav")
with wave.open(ding_wav_path, "rb") as wav_file:
    ding_wav_data = wav_file.readframes(-1)
    ding_wav_frame_rate = wav_file.getframerate()
    ding_wav_channels = wav_file.getnchannels()
    ding_wav_frame = OutputAudioRawFrame(ding_wav_data, ding_wav_frame_rate, ding_wav_channels)




class ActionSoundPlayer(FrameProcessor):
    def __init__(self, sound_frame):
        super().__init__()
        self.sound_frame = sound_frame

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, ActionFrame):
            # Push the sound frame downstream
            await self.push_frame(self.sound_frame)
        else:
            await self.push_frame(frame, direction)




# Load environment variables
load_dotenv(override=True)

logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | <level>{message}</level>",
    level="DEBUG",
)
logger.add("logs/voxagent.log", rotation="10 MB")

async def main(room_url, token):
    """Main function to set up and run the agent pipeline."""
    transport = DailyTransport(
        room_url,
        token,
        "FitnessAgent",
        DailyParams(
            audio_out_enabled=True,
            vad_enabled=True,
            transcription_enabled=True,
        ),
    )

    tts = OpenAITTSService(api_key=os.getenv("OPENAI_API_KEY"), voice="shimmer")

    agent = FitnessOnboardingAgent()

    messages = agent.messages

    tma_in = LLMUserResponseAggregator(messages)
    tma_out = LLMAssistantResponseAggregator(messages)

    llm = CustomOpenAILLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        model=os.getenv("OPENAI_MODEL", "gpt-4o"),
        functions=agent.functions,
        function_call="auto",
    )

    # Register functions with their handlers
    for function in agent.functions:
        function_name = function['name']
        handler = getattr(agent, function_name, None)
        if handler:
            llm.register_function(function_name, handler)
        else:
            logger.warning(f"No handler found for function '{function_name}'")


    # Instantiate the ActionSoundPlayer
    action_sound_player = ActionSoundPlayer(ding_wav_frame)

    pipeline = Pipeline(
        [
            transport.input(),
            tma_in,
            llm,
            action_sound_player,  # Add this processor
            tts,
            transport.output(),
            tma_out,
        ]
    )

    task = PipelineTask(pipeline, PipelineParams(allow_interruptions=True))

    @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant):
        logger.info(f"Participant {participant['id']} joined the room.")
        transport.capture_participant_transcription(participant["id"])
        # Start conversation by queuing the initial system message
        await task.queue_frames([LLMMessagesFrame([messages[0]])])

    runner = PipelineRunner()
    await runner.run(task)

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="VoxAgent Runner")
    parser.add_argument(
        "-u", "--room_url", type=str, required=True, help="Daily.co room URL"
    )
    parser.add_argument(
        "-t", "--token", type=str, required=True, help="Daily.co token"
    )

    args = parser.parse_args()

    asyncio.run(main(args.room_url, args.token))
