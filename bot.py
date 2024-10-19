from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.openai import OpenAILLMService
from pipecat.frames.frames import LLMMessagesFrame
from loguru import logger
from colorama import Fore, Style
import os

class FitnessOnboardingAgent:
    def __init__(self):
        logger.info("Initializing FitnessOnboardingAgent...")
        self.messages = [
            {
                "role": "system",
                "content": (
                    "You are a helpful assistant in a WebRTC call. "
                    "When you want to perform an action, write it on a new line enclosed in angle brackets, like <wave_hand>. "
                    "Do not include these actions in your spoken output. "
                    "Your spoken response should be clear and free of any action tokens."
                ),
            },
        ]

        self.functions = [
            {
                "name": "verify_name",
                "description": "Verify the user's name.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "The user's name"},
                    },
                    "required": ["name"],
                },
            },
            # Add more function definitions as needed
        ]

    async def function_handler(self, function_name, tool_call_id, args, llm, context, result_callback):
        """Handle function calls from the LLM."""
        if function_name == "verify_name":
            await self.verify_name(args, result_callback)
        # Handle other functions similarly

    async def verify_name(self, function_name, tool_call_id, args, llm, context, result_callback):
        """Handle the 'verify_name' function call."""
        name = args.get("name")
        if name:
            logger.info(f"Verified Name: {name}")
            # Simulate database write
            await result_callback(
                [
                    {
                        "role": "assistant",
                        "content": f"Nice to meet you, {name}! How old are you?",
                    }
                ]
            )
        else:
            logger.warning("No name provided for verification.")
            await result_callback(
                [
                    {
                        "role": "assistant",
                        "content": "I didn't catch your name. Could you please repeat it?",
                    }
                ]
            )
