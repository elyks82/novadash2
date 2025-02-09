import os
import asyncio
import websockets
import json
import httpx
from aiofiles import open as aio_open
from pydantic import SecretStr


class NovadashClient:
    def __init__(self, stage=False):
        if stage:
            self.api_url = "staging.api.novadash.art"
            self.tools_api_url = "novadashartlab--tools-dev-fastapi-app-dev.modal.run" 
        else:
            self.api_url = "api.novadash.art"
            self.tools_api_url = "novadashartlab--tools-fastapi-app.modal.run"
        self.api_key = get_api_key()

    def create(self, workflow, args):        
        return asyncio.run(self.async_create(workflow, args))
    
    async def async_create(self, workflow, args):
        uri = f"https://{self.api_url}/v2/tasks/create"
        headers = {"X-Api-Key": self.api_key.get_secret_value()}
        payload = {"workflow": workflow, "args": args}
        
        # try:
        if 1:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(uri, headers=headers, json=payload)
                response.raise_for_status()
                # print(response)
                # print(response.content)
                # print(response.json())
                task_id = response.json().get("task", {}).get("_id")
                async for event in self._subscribe(task_id):
                    if event["status"] == "completed":
                        return event["result"]
                    if event["status"] == "failed":
                        raise Exception("Error occurred while processing task")
        # except httpx.HTTPStatusError as e:
        #     raise Exception(f"HTTP error occurred: {e.response.status_code} - {e.response.text}")
        # except Exception as e:
        #     raise Exception(f"An error occurred: {str(e)}")

    async def _subscribe(self, task_id):
        url = f"https://{self.api_url}/v2/tasks/events?taskId={task_id}"
        headers = {"X-Api-Key": self.api_key.get_secret_value()}
        
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                async with client.stream("GET", url, headers=headers) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line:
                            continue
                        if line.startswith("event:"):
                            event_data = line[6:].strip()
                        elif line.startswith("data:") and event_data == 'task-update':
                            yield json.loads(line[6:])
        except httpx.HTTPStatusError as e:
            raise Exception(f"HTTP error occurred: {e.response.status_code} - {e.response.text}")
        except Exception as e:
            raise Exception(f"An error occurred: {str(e)}")
        
    def get_or_create_thread(self, thread_name):
        uri = f"https://{self.tools_api_url}/thread/create"
        headers = {"X-Api-Key": self.api_key.get_secret_value()}
        payload = {"name": thread_name}
        # try:
        if 1:
            with httpx.Client(timeout=60) as client:
                response = client.post(uri, headers=headers, json=payload)
                response.raise_for_status()
                response = response.json()
                thread_id = response.get("thread_id")
                return thread_id
        # except httpx.HTTPStatusError as e:
        #     raise Exception(f"HTTP error occurred: {e.response.status_code} - {e.response.text}")
        # except Exception as e:
        #     raise Exception(f"An error occurred: {str(e)}")
        
    def chat(self, message, thread_id, agent_id):
        async def consume_chat():
            return [message async for message in self.async_chat(message, thread_id, agent_id)]
        return asyncio.run(consume_chat())

    async def async_chat(self, message, thread_id, agent_id):
        payload = {
            "message": message,
            "thread_id": thread_id,
            "agent_id": agent_id
        }
        async for response in self.async_run_ws("/ws/chat", payload):
            yield response

    # should come up with a better way to do this
    async def async_discord_chat(self, message, thread_id, channel_id):
        payload = {
            "message": message,
            "thread_id": thread_id,
            "channel_id": channel_id
        }
        async for response in self.async_run_ws("/ws/chat/discord", payload):
            yield response

    def get_discord_channels(self):
        uri = f"https://{self.tools_api_url}/chat/discord/channels"
        headers = {"X-Api-Key": self.api_key.get_secret_value()}
        with httpx.Client(timeout=60) as client:
            response = client.post(uri, headers=headers, json={})
            response.raise_for_status()
            return response.json()

    async def async_run(self, endpoint, payload):
        uri = f"https://{self.tools_api_url}{endpoint}"
        headers = {"X-Api-Key": self.api_key.get_secret_value()}
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(uri, headers=headers, json=payload)
            response.raise_for_status()
            return response.json()

    async def async_run_ws(self, endpoint, payload):
        uri = f"wss://{self.tools_api_url}{endpoint}"
        headers = {"X-Api-Key": self.api_key.get_secret_value()}
        # try:
        if 1:
            async with websockets.connect(uri, extra_headers=headers) as websocket:                
                await websocket.send(json.dumps(payload))
                async for message in websocket:
                    message_data = json.loads(message)
                    yield message_data
        # except websockets.exceptions.ConnectionClosed as e:
        #    print(f"Connection closed by the server with code: {e.code}")
        # except Exception as e:
        #    print(f"Error: {e}")

    def upload(self, file_path):
        return asyncio.run(self.async_upload(file_path))

    async def async_upload(self, file_path):
        async with aio_open(file_path, "rb") as f:
            media = await f.read()
            headers = {"x-api-key": self.api_key.get_secret_value()}
            files = {"media": ("media", media)}
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    f"https://{self.api_url}/media/upload",
                    headers=headers,
                    files=files,
                )
            return response.json()


def get_api_key() -> SecretStr:
    if os.getenv("NOVADASH_API_KEY"):
        return SecretStr(os.getenv("NOVADASH_API_KEY"))
    home_dir = os.path.expanduser("~")
    api_key_file = os.path.join(home_dir, ".novadash")
    try:
        with open(api_key_file, "r") as file:
            api_key = file.read().strip()
        return SecretStr(api_key)
    except FileNotFoundError:
        raise Exception("\033[91mNo NOVADASH_API_KEY found. Please set it in your environment or run `novadash login` to save it in your home directory.\033[0m")
