<div align="center">
  <h1>️ Actual vs Planned Bot</h1>
  <p>A lightweight, personal Telegram bot for hour-by-hour timeline tracking, productivity analytics, and todo management.</p>

  <p>
    <a href="https://www.python.org"><img src="https://img.shields.io/badge/Python-3.10%2B-blue?style=flat-square&logo=python" alt="Python 3.10+" /></a>
    <a href="https://telegram.org"><img src="https://img.shields.io/badge/Telegram-Bot%20API-0088cc?style=flat-square&logo=telegram" alt="Telegram API" /></a>
    <a href="https://termux.dev"><img src="https://img.shields.io/badge/Platform-Termux%20%7C%20Linux%20%7C%20Docker-black?style=flat-square" alt="Platform" /></a>
    <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green.svg?style=flat-square" alt="License" /></a>
  </p>
</div>

---

## Overview

**Actual vs Planned Bot** is an hourly accountability companion built to help eliminate daily drift. It tracks scheduled tasks versus actual logged execution, maintains prioritized todo backlogs, and generates productivity summaries. 

Designed with zero unnecessary overhead, it runs seamlessly 24/7 on low-power devices, home servers, or directly inside Android Termux.

---

## ️ Installation

### 1. Prerequisites
- **Python**: 3.9, 3.10, or 3.11
- **Telegram Bot Token**: Created via [@BotFather](https://t.me/BotFather)

### 2. Standard Setup
```bash
# Clone the repository
git clone https://github.com/danMoksh/actualplannedbot.git
cd actualplannedbot

# Set up virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Termux Setup (Android)
```bash
pkg update && pkg install python git
git clone https://github.com/danMoksh/actualplannedbot.git
cd actualplannedbot
pip install -r requirements.txt
```

### 4. Configuration
Create a `.env` file in the project directory:
```ini
TELEGRAM_BOT_TOKEN=your_token_here
AUTHORIZED_USER_ID=your_telegram_id_here
```

---

## Usage

### Running Locally
```bash
python3 bot.py
```

### Running with Docker
```bash
docker build -t actualplannedbot .
docker run -d --name actualplanned --restart unless-stopped actualplannedbot
```

---

## Security

For security reporting and vulnerability handling, see [SECURITY.md](SECURITY.md).

---

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
