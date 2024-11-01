from novadash.client import NovadashClient

client = NovadashClient(stage=True)

result = client.create("chat", {
    "content": "can you controlnet this into some kind of cyberpunk style?",
    "attachments": ["https://novadashartlab-stage-data.s3.us-east-1.amazonaws.com/62946527441201f82e0e3d667fda480e176e9940a2e04f4e54c5230665dfc6f6.jpg"],
    "thread_id": "66f1fd08e09afe5a7c7f295d",
    "agent_id": "66f1c7b4ee5c5f46bbfd3cb8"
})



print(result)