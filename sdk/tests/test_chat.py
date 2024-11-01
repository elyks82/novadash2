from novadash import NovadashClient

novadash_client = NovadashClient()

thread_id = novadash_client.get_or_create_thread("test_thread_anthro")
print(thread_id)

response = novadash_client.chat(
    thread_id=thread_id, 
    message={
        "content": "make a picture of a dog with a dark grittier style",  
        "settings": {}, 
        "attachments": []
    }
)

print(response)

