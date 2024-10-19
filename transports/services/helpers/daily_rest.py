import aiohttp
from dataclasses import dataclass
import time

@dataclass
class DailyRoomParams:
    """Parameters for creating a Daily.co room."""
    name: str = None
    privacy: str = "public"  # or "private"
    properties: dict = None  # Room configuration properties

class DailyRESTHelper:
    def __init__(self, daily_api_key, daily_api_url, aiohttp_session):
        self.daily_api_key = daily_api_key
        self.daily_api_url = daily_api_url
        self.session = aiohttp_session
        self.headers = {
            "Authorization": f"Bearer {self.daily_api_key}",
            "Content-Type": "application/json",
        }

    async def create_room(self, params: DailyRoomParams):
        """Create a new room on Daily.co."""
        url = f"{self.daily_api_url}/rooms"

        payload = {}
        if params.name:
            payload["name"] = params.name
        if params.privacy:
            payload["privacy"] = params.privacy
        if params.properties:
            payload["properties"] = params.properties

        async with self.session.post(url, json=payload, headers=self.headers) as response:
            if response.status == 200 or response.status == 201:
                room_data = await response.json()
                return room_data
            else:
                error_text = await response.text()
                raise Exception(f"Failed to create room: {response.status} - {error_text}")

    async def get_token(self, room_name, expiry_time: int = 3600):
        """Generate a token for the specified room with an expiration time."""
        url = f"{self.daily_api_url}/meeting-tokens"

        current_time = int(time.time())
        exp = current_time + expiry_time  # Expiration time in seconds

        payload = {
            "properties": {
                "room_name": room_name,
                "exp": exp,  # Expiry time in seconds since epoch
                "is_owner": True,  # Set to True to grant admin privileges
            }
        }

        async with self.session.post(url, json=payload, headers=self.headers) as response:
            if response.status == 200 or response.status == 201:
                token_data = await response.json()
                return token_data.get("token")
            else:
                error_text = await response.text()
                raise Exception(f"Failed to get token: {response.status} - {error_text}")

