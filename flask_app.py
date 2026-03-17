from dotenv import load_dotenv

from webapp import create_app

load_dotenv()  # loads .env if present
app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)

