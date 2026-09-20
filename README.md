An automated tool to bridge Telegram channels with Facebook Pages. It listens to incoming messages, rewrites the content using AI (OpenRouter / Gemini / Groq), and publishes/queues posts for Facebook.

## 🚀 Features
- **Multi-Tenant SaaS Readiness:** Isolated workspaces & configurations per user.
- **Telegram MTProto Integration:** Listens to channel posts in real-time.
- **Flexible AI Support:** Supports Gemini, Llama 3, DeepSeek, or any OpenRouter model.
- **Manual / Automatic Approval Modes:** Control posts before they go live.
- **Full Dashboard UI:** EN/AR i18n support with live logs and diagnostic connection tests.

## 🛠️ Quick Start

1. **Clone the repository:**
   ```bash
   git clone [https://github.com/hamkes/tg-fb-autoposter.git](https://github.com/hamkes/tg-fb-autoposter.git)
   cd tg-fb-autoposter
Install dependencies:

Bash
pip install -r requirements.txt
Create Admin Account:

Bash
python create_admin.py admin@example.com your_password
Run the App:

Bash
python app.py
Access the dashboard at http://localhost:5000.

🤝 Contributing
Contributions, issues, and feature requests are welcome! Feel free to check the issues page.
