from novadash import NovadashClient

novadash_client = NovadashClient()

args = {   
    "prompt": "A professional photo of <concept> as a fashion model looking fresh, beautiful, youthful, easeful, bright.",
    "lora": "66904ec042b902d8eb3b41e6",
    "look_image": "https://storage.googleapis.com/public-assets-xander/Random/remove/xhibit/test2.jpeg",
    "face_image": "https://storage.googleapis.com/public-assets-xander/Random/remove/xhibit/face.jpeg",
    "resolution": 1152
}

response = novadash_client.create(
    workflow="xhibit_vton",
    args=args
)

print(response)
