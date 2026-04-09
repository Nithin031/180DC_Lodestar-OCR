from google import genai
import os

client = genai.Client(api_key="AIzaSyC_ERRLsTGtTezU6BOgI1LRbiO_yVIpq3U")

for model in client.models.list():
    print(model.name, model.supported_generation_methods)