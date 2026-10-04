import os
from datetime import datetime, timezone
import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, request
from flask_cors import CORS
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, DuplicateKeyError, PyMongoError
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

app = Flask(__name__)
CORS(app, supports_credentials=True)

API_KEY = os.getenv("OPENAI_API_KEY")
MONGODB_URI = os.getenv("MONGODB_URI")
MONGODB_DB_NAME = os.getenv("MONGODB_DB_NAME", "healthcare_ai")

mongo_client = None
users_collection = None
chat_history_collection = None
mongo_error_message = ""

if MONGODB_URI:
    try:
        mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
        mongo_client.admin.command("ping")
        users_collection = mongo_client[MONGODB_DB_NAME]["users"]
        chat_history_collection = mongo_client[MONGODB_DB_NAME]["chat_history"]
        users_collection.create_index("username", unique=True)
        chat_history_collection.create_index([("username", 1), ("created_at", 1)])
    except (ConnectionFailure, PyMongoError):
        mongo_client = None
        users_collection = None
        chat_history_collection = None
        mongo_error_message = "MongoDB connection failed. Check that MONGODB_URI is a valid MongoDB connection string."


def get_json_value(data, key):
    value = (data or {}).get(key, "")
    return value.strip() if isinstance(value, str) else ""


def mongo_not_configured_response():
    message = mongo_error_message or "MongoDB is not configured. Add MONGODB_URI to your .env file."
    return jsonify({"message": message}), 500


def auth_not_configured_response():
    if users_collection is None or chat_history_collection is None:
        return mongo_not_configured_response()
    return None


def mongo_error_reply():
    return mongo_error_message or "MongoDB is not configured. Add MONGODB_URI to your .env file."


@app.route("/")
def home():
    return "Healthcare AI server is running."


@app.route("/register", methods=["POST"])
def register():
    auth_error = auth_not_configured_response()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True) or {}
    username = get_json_value(data, "username")
    gender = get_json_value(data, "gender")
    password = data.get("password", "")
    age = data.get("age")

    if not username or not gender or not password or age in (None, ""):
        return jsonify({"message": "Username, age, gender, and password are required."}), 400

    try:
        age = int(age)
    except (TypeError, ValueError):
        return jsonify({"message": "Age must be a valid number."}), 400

    if age < 1 or age > 120:
        return jsonify({"message": "Age must be between 1 and 120."}), 400

    if len(password) < 8 or not any(char.isdigit() for char in password):
        return jsonify({"message": "Password must be at least 8 characters and include a number."}), 400

    try:
        users_collection.insert_one(
            {
                "username": username,
                "age": age,
                "gender": gender,
                "password": generate_password_hash(password),
            }
        )
        return jsonify({"message": "Registered successfully."}), 201
    except DuplicateKeyError:
        return jsonify({"message": "Username already exists."}), 409
    except PyMongoError:
        return jsonify({"message": "Could not create the account right now."}), 500


@app.route("/login", methods=["POST"])
def login():
    auth_error = auth_not_configured_response()
    if auth_error:
        return auth_error

    data = request.get_json(silent=True) or {}
    username = get_json_value(data, "username")
    password = data.get("password", "")

    if not username or not password:
        return jsonify({"message": "Username and password are required."}), 400

    try:
        user = users_collection.find_one({"username": username})
    except PyMongoError:
        return jsonify({"message": "Could not check your account right now."}), 500

    if not user or not check_password_hash(user.get("password", ""), password):
        return jsonify({"message": "Invalid username or password."}), 401

    return jsonify({"message": "Login successful."})


@app.route("/history/<username>", methods=["GET"])
def get_history(username):
    auth_error = auth_not_configured_response()
    if auth_error:
        return auth_error

    username = username.strip()
    if not username:
        return jsonify({"message": "Username is required."}), 400

    try:
        history = list(
            chat_history_collection.find(
                {"username": username},
                {"_id": 0, "sender": 1, "text": 1},
            ).sort("created_at", 1)
        )
        return jsonify({"history": history})
    except PyMongoError:
        return jsonify({"message": "Could not load chat history right now."}), 500


@app.route("/history/<username>", methods=["DELETE"])
def clear_history(username):
    auth_error = auth_not_configured_response()
    if auth_error:
        return auth_error

    username = username.strip()
    if not username:
        return jsonify({"message": "Username is required."}), 400

    try:
        chat_history_collection.delete_many({"username": username})
        return jsonify({"message": "Chat history cleared."})
    except PyMongoError:
        return jsonify({"message": "Could not clear chat history right now."}), 500


@app.route("/chat", methods=["POST"])
def chat():
    auth_error = auth_not_configured_response()
    if auth_error:
        return jsonify({"reply": mongo_error_reply()}), 500

    data = request.get_json(silent=True) or {}
    user_message = data.get("message", "")
    username = get_json_value(data, "username")
    language = get_json_value(data, "language").lower() or "en"
    mode = get_json_value(data, "mode").lower() or "general"

    if not API_KEY:
        return jsonify({"reply": "Error: API key not found in environment."}), 500

    if not username:
        return jsonify({"reply": "Please log in again before chatting."}), 400

    if not isinstance(user_message, str) or not user_message.strip():
        return jsonify({"reply": "Please enter a message."}), 400

    # Set system prompt based on language
    if language == "hi":
        system_prompt = "आप एक सहायक स्वास्थ्य सहायक हैं। सभी उत्तर हिंदी में दें।"
    else:
        system_prompt = "You are a helpful healthcare assistant."

    mode_prompts = {
        "symptom": "Focus on symptom understanding, likely causes, and when the user should seek medical care.",
        "prediction": "Discuss likely conditions carefully, avoid certainty, and advise medical evaluation for diagnosis.",
        "precaution": "Focus on home care, precautions, prevention, and red-flag symptoms that need a doctor.",
        "awareness": "Focus on health education, lifestyle guidance, and prevention tips.",
        "general": "Provide general healthcare guidance with a calm and supportive tone."
    }
    system_prompt = f"{system_prompt} {mode_prompts.get(mode, mode_prompts['general'])}"

    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
                "HTTP-Referer": "http://localhost:5000",
                "X-Title": "CareaAI App",
            },
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
            },
            timeout=30,
        )

        if response.status_code != 200:
            return jsonify({"reply": f"API Error: {response.text}"}), response.status_code

        result = response.json()
        reply = result.get("choices", [{}])[0].get("message", {}).get("content", "No response")

        chat_history_collection.insert_many(
            [
                {"username": username, "sender": "user", "text": user_message, "created_at": datetime.now(timezone.utc)},
                {"username": username, "sender": "ai", "text": reply, "created_at": datetime.now(timezone.utc)},
            ]
        )

        return jsonify({"reply": reply})
    except requests.RequestException as exc:
        return jsonify({"reply": f"Network Error: {exc}"}), 502
    except PyMongoError:
        return jsonify({"reply": "Chat worked, but saving the history failed."}), 500
    except Exception as exc:
        return jsonify({"reply": f"Server Error: {exc}"}), 500


if __name__ == "__main__":
    app.run(debug=True)
