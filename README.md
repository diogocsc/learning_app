# 📘 AI Learning Assistant

An AI-powered study tool that transforms your PDF documents into flashcards, multiple-choice questions, and short-answer quizzes — with spaced repetition to help you retain what you learn.

🔗 **Live demo:** [study.diogocordeiro.pt](https://study.diogocordeiro.pt)

---

## Features

- **PDF ingestion** — upload any PDF and the app extracts and chunks the text automatically
- **AI-generated cards** — flashcards, multiple-choice, and short-answer questions generated via LLM from your documents
- **RAG pipeline** — documents are embedded and stored in a FAISS vector store, enabling context-aware card generation
- **Spaced repetition (SRS)** — cards are scheduled using a SM-2-inspired algorithm so you review them at the right time
- **Quiz mode** — test yourself with multiple-choice and short-answer questions with instant feedback
- **Progress tracking** — per-subject stats showing attempts, correct answers, and accuracy per card
- **User authentication** — secure registration and login with bcrypt password hashing
- **Manual card creation** — add your own flashcards alongside AI-generated ones
- **Admin panel** — admin user management with impersonation support and audit logging
- **Dockerised** — ready to self-host with the included Dockerfile

---

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend & app | [Streamlit](https://streamlit.io) |
| LLM | [Ollama](https://ollama.com) (cloud API, `gpt-oss:120b`) |
| Embeddings | [sentence-transformers](https://www.sbert.net/) |
| Vector store | [FAISS](https://faiss.ai/) |
| PDF parsing | [PyMuPDF](https://pymupdf.readthedocs.io/) |
| Database | SQLite |
| Auth | bcrypt |
| Containerisation | Docker |

---

## Getting Started

### Prerequisites

- Python 3.10+
- An [Ollama](https://ollama.com) API key

### Installation

```bash
git clone https://github.com/diogocsc/learning_app.git
cd learning_app
pip install -r requirements.txt
```

### Configuration

Create a `.streamlit/secrets.toml` file (never commit this):

```toml
OLLAMA_API_KEY = "your_ollama_api_key_here"
```

### Run locally

```bash
streamlit run app.py
```

### Run with Docker

```bash
docker build -t learning-app .
docker run -p 8501:8501 learning-app
```

---

## Project Structure

```
learning_app/
├── app.py               # Main Streamlit app and UI
├── card_generation.py   # PDF extraction and AI card generation
├── rag_store.py         # FAISS vector store and document embedding
├── llm_client.py        # Ollama API client
├── db.py                # SQLite database operations
├── models.py            # Data models
├── auth.py              # Authentication (login/register)
├── session_utils.py     # Streamlit session state helpers
├── admin_pages.py       # Admin UI pages
├── admin_utils.py       # Admin utility functions
├── config.py            # App configuration and CSS
└── Dockerfile
```

---

## Roadmap

- [ ] Support for additional document formats (DOCX, TXT)
- [ ] Exportable card decks
- [ ] Improved SRS scheduling with full SM-2 algorithm
- [ ] Multi-language support

---

## Author

**Diogo Cordeiro** — [diogocordeiro.pt](http://www.diogocordeiro.pt) · [GitHub](https://github.com/diogocsc)
