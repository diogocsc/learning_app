import json
import requests
import os

os.environ["OLLAMA_API_KEY"] = "8ef3cb30d6494a059dfae9c8830e69c8.5HQqCZoT27jrEdEFmcrm4rsl"  # Replace with your Ollama Cloud API key
OLLAMA_BASE_URL = "https://ollama.com"

headers = {
    "Content-Type": "application/json",
    "Authorization": f"Bearer {os.environ['OLLAMA_API_KEY']}"
}

# ============================================
# Interact with clould llm
# ============================================
def ask_question(question):

    # Call Ollama Cloud API
    payload = {
        "model": "gpt-oss:120b",  # llama2 Change to your preferred model (e.g., mistral, llama2)
        "prompt": question
    }

    print("\n--- Ollama Response ---\n")
    full_response = ""
    with requests.post(f"{OLLAMA_BASE_URL}/api/generate", headers=headers, json=payload, stream=True) as r:
        for line in r.iter_lines():
            if line:
                try:
                    data = json.loads(line.decode("utf-8"))
                    if "response" in data:
                        print(data["response"], end="", flush=True)
                        full_response += data["response"]
                except json.JSONDecodeError:
                    continue
    print("\n\n--- End of Response ---\n")
    return full_response